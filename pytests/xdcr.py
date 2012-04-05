import uuid
from TestInput import TestInputSingleton
import logger
import time

import unittest
import string
import urllib
import httplib
import json
from membase.api.rest_client import RestConnection, RestHelper
from membase.helper.bucket_helper import BucketOperationHelper
from membase.helper.cluster_helper import ClusterOperationHelper
from membase.helper.rebalance_helper import RebalanceHelper
from memcached.helper.data_helper import MemcachedClientHelper
from remote.remote_util import RemoteMachineShellConnection
from couchdb import client
from rebalancingtests import RebalanceDataGenerator
from memcached.helper.kvstore import ClientKeyValueStore

class XDCRBaseTest(unittest.TestCase):

    @staticmethod
    def common_setup(input, testcase):
        # Resource file has 'cluster' tag
        for key, servers in input.clusters.items():
            XDCRBaseTest.common_tearDown(servers, testcase)
            XDCRBaseTest.cluster_initialization(servers)
            XDCRBaseTest.create_buckets(servers, testcase, howmany=1)
            if len(servers) > 1:
                RebalanceHelper.rebalance_in(servers,len(servers)-1)

    @staticmethod
    def common_tearDown(servers, testcase):
        BucketOperationHelper.delete_all_buckets_or_assert(servers, testcase)
        for server in servers:
            ClusterOperationHelper.cleanup_cluster([server])
        ClusterOperationHelper.wait_for_ns_servers_or_assert(servers, testcase)

    @staticmethod
    def choose_nodes(master, nodes, howmany):
        selected = []
        for node in nodes:
            if not XDCRBaseTest.contains(node.ip, master.ip) and\
               not XDCRBaseTest.contains(node.ip, '127.0.0.1'):
                selected.append(node)
                if len(selected) == howmany:
                    break
        return selected

    @staticmethod
    def contains(string1, string2):
        if string1 and string2:
            return string1.find(string2) != -1
        return False

    @staticmethod
    def cluster_initialization(servers):
        log = logger.Logger().get_logger()
        master = servers[0]
        log.info('picking server : {0} as the master'.format(master))
        # if all nodes are on the same machine let's have the bucket_ram_ratio
        # as bucket_ram_ratio * 1/len(servers)
        node_ram_ratio = BucketOperationHelper.base_bucket_ratio(servers)
        rest = RestConnection(master)
        info = rest.get_nodes_self()
        rest.init_cluster(username=master.rest_username,
                          password=master.rest_password)
        rest.init_cluster_memoryQuota(memoryQuota =
            int(info.mcdMemoryReserved * node_ram_ratio))

    @staticmethod
    def create_buckets(servers, testcase, howmany=1, replica=1,
                       bucket_ram_ratio=(2.0 / 3.0)):
        node_ram_ratio = BucketOperationHelper.base_bucket_ratio(servers)
        master = servers[0]
        BucketOperationHelper.create_multiple_buckets(
            master, replica,node_ram_ratio * bucket_ram_ratio, howmany=howmany)
        rest = RestConnection(master)
        buckets = rest.get_buckets()
        for bucket in buckets:
            ready = BucketOperationHelper.wait_for_memcached(master,
                                                             bucket.name)
            testcase.assertTrue(ready, "wait_for_memcached failed")

    @staticmethod
    def poll_for_condition_rec(condition, sleep, num_iters):
        if num_iters == 0:
            return False
        else:
            if condition():
                return True
            else:
                time.sleep(sleep)
                return XDCRBaseTest.poll_for_condition_rec(condition, sleep,
                                                           (num_iters - 1))

    @staticmethod
    def poll_for_condition(condition, sleep, timeout):
        num_iters = timeout/sleep
        return XDCRBaseTest.poll_for_condition_rec(condition, sleep, num_iters)

    @staticmethod
    def verify_replicated_data(rest_conn, bucket, kvstore, sleep, timeout):
        # FIXME: Use ep-engine stats for verification
        def verify():
            errors = RebalanceDataGenerator.do_verification(kvstore, rest_conn,
                                                            bucket)
            if errors:
                return False
            else:
                return True

        log = logger.Logger().get_logger()
        if XDCRBaseTest.poll_for_condition(verify, sleep, timeout):
            log.info("Verified results of replication")
        else:
            log.error("Timed out waiting for replication to complete")


class XDCRTests(unittest.TestCase):
    def setUp(self):
        self.log = logger.Logger().get_logger()
        self._input = TestInputSingleton.input
        self._clusters = self._input.clusters
        self._buckets = ["bucket-0"]
        self._num_items = TestInputSingleton.input.param("num_items", 100000)
        self._poll_sleep = TestInputSingleton.input.param("poll_sleep", 5)
        self._poll_timeout = TestInputSingleton.input.param("poll_timeout", 300)
        self._params = {"sizes" : [128], "count" : self._num_items,
                        "padding":"cluster_a", "seed" : "cluster_a",
                        "bucket" : self._buckets[0]}
        self._params["kv_template"] = {"name": "employee-${prefix}-${seed}",
                                       "sequence": "${seed}","join_yr": 2007,
                                       "join_mo": 10, "join_day": 20,
                                       "email": "${prefix}@couchbase.com",
                                       "job_title": "Software Engineer-${padding}"}
        if not self._clusters:
            self.log.info("No Cluster tags defined in resource file")
            exit(1)
        if len(self._clusters.items()) == 2:
            XDCRBaseTest.common_setup(self._input, self)
        else:
            self.log.info("Two clusters needed")
            exit(1)
        self.log.info("Setup")

    def tearDown(self):
        self.log.info("Teardown")
        for id, servers in self._clusters.items():
            XDCRBaseTest.common_tearDown(servers, self)

    def test_continuous_unidirectional_sets(self):
        cluster_ref_b = "cluster_ref_a"
        master_a = self._input.clusters.get(0)[0]
        rest_conn_a = RestConnection(master_a)

        cluster_ref_b = "cluster_ref_b"
        master_b = self._input.clusters.get(1)[0]
        rest_conn_b = RestConnection(master_b)

        # Start load
        kvstore = ClientKeyValueStore()
        self._params["ops"] = "set"
        task_def = RebalanceDataGenerator.create_loading_tasks(self._params)
        load_thread = RebalanceDataGenerator.start_load(rest_conn_a,
                                                        self._buckets[0],
                                                        task_def, kvstore)
        load_thread.start()
        load_thread.join()

        # Start replication
        replication_type = "continuous"
        rest_conn_a.add_remote_cluster(master_b.ip, master_b.port,
                                       master_b.rest_username,
                                       master_b.rest_password, cluster_ref_b)
        (rep_database, rep_id) = rest_conn_a.start_replication(replication_type,
                                                               self._buckets[0],
                                                               cluster_ref_b)

        # Verify replicated data
        XDCRBaseTest.verify_replicated_data(rest_conn_b, self._buckets[0],
                                            kvstore, self._poll_sleep,
                                            self._poll_timeout)

        # Cleanup
        rest_conn_a.stop_replication(rep_database, rep_id)
        rest_conn_a.remove_remote_cluster(cluster_ref_b)


    def test_continuous_unidirectional_sets_deletes(self):
        cluster_ref_b = "cluster_ref_a"
        master_a = self._input.clusters.get(0)[0]
        rest_conn_a = RestConnection(master_a)

        cluster_ref_b = "cluster_ref_b"
        master_b = self._input.clusters.get(1)[0]
        rest_conn_b = RestConnection(master_b)

        # Start load
        kvstore = ClientKeyValueStore()
        self._params["ops"] = "set"
        task_def = RebalanceDataGenerator.create_loading_tasks(self._params)
        load_thread = RebalanceDataGenerator.start_load(rest_conn_a,
                                                        self._buckets[0],
                                                        task_def, kvstore)
        load_thread.start()
        load_thread.join()

        # Start replication
        replication_type = "continuous"
        rest_conn_a.add_remote_cluster(master_b.ip, master_b.port,
                                       master_b.rest_username,
                                       master_b.rest_password, cluster_ref_b)
        (rep_database, rep_id) = rest_conn_a.start_replication(replication_type,
                                                               self._buckets[0],
                                                               cluster_ref_b)

        # Do some deletes
        self._params["ops"] = "delete"
        self._params["count"] = self._num_items/5
        task_def = RebalanceDataGenerator.create_loading_tasks(self._params)
        load_thread = RebalanceDataGenerator.start_load(rest_conn_a,
                                                        self._buckets[0],
                                                        task_def, kvstore)
        load_thread.start()
        load_thread.join()

        # Verify replicated data
        XDCRBaseTest.verify_replicated_data(rest_conn_b, self._buckets[0],
                                            kvstore, self._poll_sleep,
                                            self._poll_timeout)

        # Cleanup
        rest_conn_a.stop_replication(rep_database, rep_id)
        rest_conn_a.remove_remote_cluster(cluster_ref_b)
