# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ducktape.services.service import Service
from ducktape.utils.util import wait_until
import subprocess, signal


class CopycatServiceBase(Service):
    """Base class for Copycat services providing some common settings and functionality"""

    logs = {
        "kafka_log": {
            "path": "/mnt/copycat.log",
            "collect_default": True},
    }

    def __init__(self, context, num_nodes, kafka, files):
        super(CopycatServiceBase, self).__init__(context, num_nodes)
        self.kafka = kafka
        self.files = files

    def pids(self, node):
        """Return process ids for Copycat processes."""
        try:
            return [pid for pid in node.account.ssh_capture("cat /mnt/copycat.pid", callback=int)]
        except:
            return []

    def set_configs(self, config_template, connector_config_templates):
        """
        Set configurations for the worker and the connector to run on
        it. These are not provided in the constructor because the worker
        config generally needs access to ZK/Kafka services to
        create the configuration.
        """
        self.config_template = config_template
        self.connector_config_templates = connector_config_templates

    def stop_node(self, node, clean_shutdown=True):
        pids = self.pids(node)
        sig = signal.SIGTERM if clean_shutdown else signal.SIGKILL

        for pid in pids:
            node.account.signal(pid, sig, allow_fail=False)
        for pid in pids:
            wait_until(lambda: not node.account.alive(pid), timeout_sec=10, err_msg="Copycat standalone process took too long to exit")

        node.account.ssh("rm -f /mnt/copycat.pid", allow_fail=False)

    def restart(self):
        # We don't want to do any clean up here, just restart the process.
        for node in self.nodes:
            self.stop_node(node)
            self.start_node(node)

    def clean_node(self, node):
        if len(self.pids(node)) > 0:
            self.logger.warn("%s %s was still alive at cleanup time. Killing forcefully..." %
                             (self.__class__.__name__, node.account))
        for pid in self.pids(node):
            node.account.signal(pid, signal.SIGKILL, allow_fail=False)

        node.account.ssh("rm -rf /mnt/copycat.pid /mnt/copycat.log /mnt/copycat.properties  " + " ".join(self.config_filenames() + self.files), allow_fail=False)

    def config_filenames(self):
        return ["/mnt/copycat-connector-" + str(idx) + ".properties" for idx, template in enumerate(self.connector_config_templates)]

class CopycatStandaloneService(CopycatServiceBase):
    """Runs Copycat in standalone mode."""

    def __init__(self, context, kafka, files):
        super(CopycatStandaloneService, self).__init__(context, 1, kafka, files)

    # For convenience since this service only makes sense with a single node
    @property
    def node(self):
        return self.nodes[0]

    def start_node(self, node):
        node.account.create_file("/mnt/copycat.properties", self.config_template)
        remote_connector_configs = []
        for idx, template in enumerate(self.connector_config_templates):
            target_file = "/mnt/copycat-connector-" + str(idx) + ".properties"
            node.account.create_file(target_file, template)
            remote_connector_configs.append(target_file)

        self.logger.info("Starting Copycat standalone process")
        with node.account.monitor_log("/mnt/copycat.log") as monitor:
            node.account.ssh("/opt/kafka/bin/copycat-standalone.sh /mnt/copycat.properties " +
                             " ".join(remote_connector_configs) +
                             " 1>> /mnt/copycat.log 2>> /mnt/copycat.log & echo $! > /mnt/copycat.pid")
            monitor.wait_until('Copycat started', timeout_sec=10, err_msg="Never saw message indicating Copycat finished startup")

        if len(self.pids(node)) == 0:
            raise RuntimeError("No process ids recorded")



class CopycatDistributedService(CopycatServiceBase):
    """Runs Copycat in distributed mode."""

    def __init__(self, context, num_nodes, kafka, files, offsets_topic="copycat-offsets", configs_topic="copycat-configs"):
        super(CopycatDistributedService, self).__init__(context, num_nodes, kafka, files)
        self.offsets_topic = offsets_topic
        self.configs_topic = configs_topic
        self.first_start = True

    def start_node(self, node):
        node.account.create_file("/mnt/copycat.properties", self.config_template)
        remote_connector_configs = []
        for idx, template in enumerate(self.connector_config_templates):
            target_file = "/mnt/copycat-connector-" + str(idx) + ".properties"
            node.account.create_file(target_file, template)
            remote_connector_configs.append(target_file)

        self.logger.info("Starting Copycat distributed process")
        with node.account.monitor_log("/mnt/copycat.log") as monitor:
            cmd = "/opt/kafka/bin/copycat-distributed.sh /mnt/copycat.properties "
            # Only submit connectors on the first node so they don't get submitted multiple times. Also only submit them
            # the first time the node is started so
            if self.first_start and node == self.nodes[0]:
                cmd += " ".join(remote_connector_configs)
            cmd += " 1>> /mnt/copycat.log 2>> /mnt/copycat.log & echo $! > /mnt/copycat.pid"
            node.account.ssh(cmd)
            monitor.wait_until('Copycat started', timeout_sec=10, err_msg="Never saw message indicating Copycat finished startup")

        if len(self.pids(node)) == 0:
            raise RuntimeError("No process ids recorded")

        self.first_start = False
