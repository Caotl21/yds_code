import os
import redis
import time
import json
import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import logging
import threading

# 设置日志记录
cuttent_time = time.strftime("%Y%m%d", time.localtime())
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename=f'migration-{cuttent_time}-1.log')
# 尝试加载集群内配置（在 Pod 中运行时）


# 连接 Redis
redis_host = os.environ.get("REDIS_HOST", "10.96.252.224")
redis_port = int(os.environ.get("REDIS_PORT", 6379))
r = redis.Redis(host=redis_host, port=redis_port)
CHANNEL = "security_alerts"

PROMETHEUS_URL = "http://10.111.12.181:9090"  # 使用你的节点IP和NodePort

def get_node_cpu_usage(node_name):
    config.load_kube_config()
    metrics_api = client.CustomObjectsApi()
    core_v1 = client.CoreV1Api()

    try:
        # 获取节点总 CPU 核数（转换为纳核）
        node = core_v1.read_node(node_name)
        cpu_total = int(node.status.capacity["cpu"]) * 10**9  # 1 core = 10^9 nancores

        # 获取当前 CPU 使用量（纳核）
        metrics = metrics_api.list_cluster_custom_object(
            "metrics.k8s.io", "v1beta1", "nodes"
        )
        for item in metrics["items"]:
            if item["metadata"]["name"] == node_name:
                cpu_usage = int(item["usage"]["cpu"].rstrip("n"))
                return min(100, (cpu_usage / cpu_total) * 100)  # 真实利用率

        return 0
    except Exception as e:
        print(f"获取指标失败: {e}")
        # logging.error(f"获取指标失败: {e}")
        return 0
    
def get_node_cpu_usage_prometheus(prometheus_url, node_ip):
    query = '(1 - avg(rate(node_cpu_seconds_total{mode="idle"}[10s])) by (instance)) *100'
    # 查询该节点1分钟内的 CPU 使用率（单位核）
    try:
        response = requests.get(f"{prometheus_url}/api/v1/query", 
                              params={'query': query}, timeout=10)
        # print(f"查询: {query}")
        # print(f"响应状态: {response.status_code}")
        if response.status_code == 200:
            result = response.json()
            # print(f"返回数据: {result}")
            for item in result['data']['result']:
                item_node_ip = item['metric'].get('instance').split(':')[0]  # 获取IP地址部分
                if item_node_ip == node_ip:
                    return float(item['value'][1])
            print(f"未找到节点 {node_ip} 的 CPU 使用率")
            return 0.0
        else:
            print(f"查询失败: {response.text}")
        return -1
    except Exception as e:
        print(f"查询Prometheus失败: {e}")
        return -1

def get_nodes():
    config.load_kube_config()
    v1 = client.CoreV1Api()
    try:
        nodes = v1.list_node().items
        return [node.metadata.name for node in nodes]
    except ApiException as e:
        print(f"获取节点列表失败: {e}")
        logging.error(f"获取节点列表失败: {e}")
        return []

def get_node_name(node):
    """获取节点名称"""
    return node.metadata.name if hasattr(node, 'metadata') and hasattr(node.metadata, 'name') else None

def get_node_ip(node):
    """获取节点IP地址"""
    if hasattr(node, 'status') and hasattr(node.status, 'addresses'):
        for address in node.status.addresses:
            if address.type == "InternalIP":
                return address.address
    return None

def get_node_status(node):
    """获取节点状态"""
    status = {}
    try:
        if hasattr(node, 'status') and hasattr(node.status, 'conditions'):
            for condition in node.status.conditions:
                if condition.type in ["Ready", "MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"]:
                    status[condition.type] = condition.status
    except Exception as e:
        print(f"获取节点状态失败: {e}")
        logging.error(f"获取节点状态失败: {e}")
    return status

def get_node_user_labels(node):
    """获取节点用户自定义标签"""
    labels = node.metadata.labels
    for key, value in labels.items():
        if key in ["computing_device", "sensing_device"]:
            return key, value
    return None, None

def migrate_pod(apps_v1, node_name, pod, target_node_selector):
    migrate_flag = False

    if target_node_selector:
        logging.info(f"迁移节点 {node_name} 的 Pod {pod.metadata.name}")
        print(f"迁移节点 {node_name} 的 Pod {pod.metadata.name}")
        namespace = pod.metadata.namespace
        owner_refs = pod.metadata.owner_references

        if not owner_refs:
            print("该 Pod 没有控制器，不能自动修改 Deployment")
            logging.warning("该 Pod 没有控制器，不能自动修改 Deployment")
            return

        for owner in owner_refs:
            if owner.kind == "ReplicaSet":
                try:
                    replicaset = apps_v1.read_namespaced_replica_set(owner.name, namespace)
                    for rs_owner in replicaset.metadata.owner_references:
                        if rs_owner.kind == "Deployment":
                            deployment_name = rs_owner.name
                            print(f"对应的 Deployment 名称为: {deployment_name}")

                            deployment = apps_v1.read_namespaced_deployment(deployment_name, namespace)

                            # 修改 nodeSelector
                            deployment.spec.template.spec.node_selector = target_node_selector

                            # 修改 CPU 限制
                            # for container in deployment.spec.template.spec.containers:
                            #     if container.name == "processor":
                            #         if not container.resources:
                            #             container.resources = client.V1ResourceRequirements()
                            #         if not container.resources.limits:
                            #             container.resources.limits = {}
                            #         container.resources.limits["cpu"] = new_cpu_limit

                            # 应用更改
                            apps_v1.patch_namespaced_deployment(
                                name=deployment_name,
                                namespace=namespace,
                                body=deployment
                            )
                            migrate_flag = True
                            print(f"Pod {pod.metadata.name} 已迁移到 {target_node_selector}")
                            logging.info(f"Pod {pod.metadata.name} 已迁移到 {target_node_selector}")
                except Exception as e:
                    print(f"迁移过程中出错: {e}")
                    logging.error(f"迁移过程中出错: {e}")

    return migrate_flag

def delete_malicious_pod_and_node(pod_name, namespace):
    # 加载 kubeconfig（在容器里使用 load_incluster_config）
    config.load_kube_config()
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()

    # 获取 Pod 详情
    try:
        pod = core_api.read_namespaced_pod(pod_name, namespace)
        node_name = pod.spec.node_name
        print(f"[INFO] Pod {pod_name} is running on node {node_name}")
    except ApiException as e:
        print(f"[ERROR] Failed to get Pod: {e}")
        return

    # Step 1: 删除 Pod（立即删除）
    try:
        core_api.delete_namespaced_pod(
            name=pod_name,
            namespace=namespace,
            grace_period_seconds=0,
            body=client.V1DeleteOptions()
        )
        print(f"[INFO] Deleted Pod {pod_name}")
        logging.info(f"Deleted Pod {pod_name}")
    except ApiException as e:
        print(f"[ERROR] Failed to delete Pod: {e}")

    # Step 2: 查找控制器并删除或标记
    owner_refs = pod.metadata.owner_references or []
    for owner in owner_refs:
        kind = owner.kind
        name = owner.name
        print(f"[INFO] Pod is owned by {kind} '{name}'")

        try:
            if kind == "ReplicaSet":
                rs = apps_api.read_namespaced_replica_set(name, namespace)
                parent_refs = rs.metadata.owner_references or []
                for p in parent_refs:
                    if p.kind == "Deployment":
                        # 控制器是 Deployment
                        print(f"[INFO] ReplicaSet belongs to Deployment '{p.name}'")
                        # 标记 Deployment 副本数为 0，防止重建
                        apps_api.patch_namespaced_deployment_scale(
                            name=p.name,
                            namespace=namespace,
                            body={"spec": {"replicas": 0}}
                        )
                        print(f"[INFO] Scaled down Deployment '{p.name}' to 0 replicas")
                    else:
                        # 直接删除 ReplicaSet
                        apps_api.delete_namespaced_replica_set(name, namespace)
                        print(f"[INFO] Deleted ReplicaSet '{name}'")

            elif kind == "Deployment":
                # 标记 Deployment 副本为 0
                apps_api.patch_namespaced_deployment_scale(
                    name=name,
                    namespace=namespace,
                    body={"spec": {"replicas": 0}}
                )
                print(f"[INFO] Scaled down Deployment '{name}' to 0 replicas")

            elif kind == "DaemonSet":
                # 删除 DaemonSet
                apps_api.delete_namespaced_daemon_set(name, namespace)
                print(f"[INFO] Deleted DaemonSet '{name}'")

            elif kind == "StatefulSet":
                apps_api.patch_namespaced_stateful_set_scale(
                    name=name,
                    namespace=namespace,
                    body={"spec": {"replicas": 0}}
                )
                print(f"[INFO] Scaled down StatefulSet '{name}' to 0 replicas")

        except ApiException as e:
            print(f"[ERROR] Failed handling controller {kind} '{name}': {e}")

    pods_monitoring = core_api.list_namespaced_pod(
        namespace="monitoring",
        field_selector=f"spec.nodeName={node_name}"
    )
    target_node_selector = {"computing_device": "compute_node2"}
    for pod in pods_monitoring.items:
        if "prometheus" in pod.metadata.name:
            pod_name_uid = pod.metadata.name
            pod_name = pod_name_uid.split("-")[:-2]
            pod_name = "-".join(pod_name)
            migrate_pod(apps_api, node_name, pod, target_node_selector)
            logging.info(f"Pod {pod.metadata.name} 已迁移到 {target_node_selector}")

    # Step 3: 删除节点
    try:
        core_api.delete_node(name=node_name)
        r.publish("node_status", json.dumps({
                "timestamp": time.strftime("%Y%m%d-%H%M%S", time.localtime()),
                "node_name": node_name,
                "status": "Ready"
            }))
        print(f"[INFO] Deleted node '{node_name}' from cluster")
        logging.info(f"Deleted node '{node_name}' from cluster")
        r.publish("node_info", json.dumps({
                    "name": node_name,
                    "status": "Not Ready",
                }))

    except ApiException as e:
        print(f"[ERROR] Failed to delete node: {e}")

def initialize_node_info():
    config.load_kube_config()
    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    nodes = v1.list_node()

    node_info = {}
    for node in nodes.items:
        label_key, label_value = get_node_user_labels(node)
        node_info[node.metadata.name] = {
            "node": node,
            "status": get_node_status(node),
            "cpu_usage": get_node_cpu_usage(node.metadata.name),
            "label_key": label_key,
            "label_value": label_value,
            "migration_pod": [],
            "is_migration": False
        }

    return node_info


def schedule_policy(node_info, pod_restart_counts):
    config.load_kube_config()
    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    nodes = v1.list_node()

    node_names = []
    node_is_ready = []
    node_cpu_usage_list = []
    node_pod_names = {}

    need_migrate_nodes = []

    
    pods = v1.list_namespaced_pod(namespace="default")
    for pod in pods.items:
        pod_name = pod.metadata.name
        node_name = pod.spec.node_name if pod.spec.node_name else "未分配"
        if pod_name not in pod_restart_counts:
            pod_restart_counts[pod_name] = 0
            logging.info(f"初始化 Pod {pod_name} 的重启计数为 0")
        restart_count = pod.status.container_statuses[0].restart_count if pod.status.container_statuses else 0
        if restart_count > pod_restart_counts[pod_name]:
            pod_restart_counts[pod_name] = restart_count
            logging.info(f"Pod {pod_name} 重启计数更新为 {restart_count}")
            r.publish("node_status", json.dumps({
                "timestamp": time.strftime("%Y%m%d-%H%M%S", time.localtime()),
                "node_name": f"{node_name}-{pod_name}",
                "status": "CrashLoopBackOff"
            }))
            time.sleep(0.1)
            r.publish("node_status", json.dumps({
                "timestamp": time.strftime("%Y%m%d-%H%M%S", time.localtime()),
                "node_name": f"{node_name}-{pod_name}",
                "status": "Ready"
            }))

    
    for node in nodes.items:
        labels = node.metadata.labels
        if "node-role.kubernetes.io/control-plane" in labels or "node-role.kubernetes.io/master" in labels:
            continue
        node_name = get_node_name(node)
        # node_cpu_usage = get_node_cpu_usage(node_name)
        node_ip = get_node_ip(node)
        node_cpu_usage = get_node_cpu_usage_prometheus(PROMETHEUS_URL, node_ip)
        # print(f'prometheus: {node_cpu_usage}')
        # if node_cpu_usage == -1:
        # node_cpu_usage = get_node_cpu_usage(node_name)
        # print(f'metrics: {node_cpu_usage}')
        node_status = get_node_status(node)

        # 判断节点上的pod是否被迁移过
        if node_info[node_name]["is_migration"]:
            logging.info(f"节点 {node_name} 已经迁移过 Pod，检查当前状态")
            print(f"节点 {node_name} 异常， 已经迁移过 Pod，检查当前状态")
            if node_info[node_name]["status"]["Ready"] == "True" and node_cpu_usage <= 60:
                print(f"节点 {node_name} 状态正常，准备迁回pod!")
                logging.info(f"节点 {node_name} 状态正常，准备迁回pod!")
                node_data = {
                    "timestamp": time.strftime("%Y%m%d-%H%M%S", time.localtime()),
                    "node_name": node_name,
                    "status": "Ready"
                }
                r.publish("node_status", json.dumps(node_data))
                source_node_name = node_info[node_name]["target_node"]
                pods = v1.list_namespaced_pod(
                    namespace="default"
                )

                pods_monitoring = v1.list_namespaced_pod(
                    namespace="monitoring"
                )
                for pod in pods.items:
                    pod_name_uid = pod.metadata.name
                    pod_name = pod_name_uid.split("-")[:-2]
                    pod_name = "-".join(pod_name)
                    if pod_name in node_info[node_name]["migration_pod"]:
                        print(f"Pod {pod.metadata.name} 已经迁回到 {node_name}")
                        logging.info(f"Pod {pod.metadata.name} 已经迁回到 {node_name}")
                        migrate_pod(apps_v1, source_node_name, pod, {node_info[node_name]["label_key"]: node_info[node_name]["label_value"]})
                        node_info[node_name]["migration_pod"].remove(pod_name)

                        node_info[node_name]["is_migration"] = False
                        node_info[node_name]["target_node"] = None
                
                for pod in pods_monitoring.items:
                    pod_name_uid = pod.metadata.name
                    pod_name = pod_name_uid.split("-")[:-2]
                    pod_name = "-".join(pod_name)
                    if pod_name in node_info[node_name]["migration_pod"]:
                        print(f"Pod {pod.metadata.name} 已经迁回到 {node_name}")
                        logging.info(f"Pod {pod.metadata.name} 已经迁回到 {node_name}")
                        migrate_pod(apps_v1, source_node_name, pod, {node_info[node_name]["label_key"]: node_info[node_name]["label_value"]})
                        node_info[node_name]["migration_pod"].remove(pod_name)

                        node_info[node_name]["is_migration"] = False
                        node_info[node_name]["target_node"] = None


        # node.status.conditions
        print(f"Node {node_name} 状态: {node_status}")
        # logging.info(f"Node {node_name} 状态: {node_status}")
        label_key, label_value = get_node_user_labels(node)

        pods = v1.list_namespaced_pod(
            namespace="default",
            field_selector=f"spec.nodeName={node_name}"
        )

        pod_names = [pod.metadata.name for pod in pods.items]
        node_pod_names[node_name] = pod_names

        node_info[node_name]["cpu_usage"] = node_cpu_usage
        node_info[node_name]["status"] = node_status
        node_info[node_name]["label_key"] = label_key
        node_info[node_name]["label_value"] = label_value

        if (node_status["Ready"] == "False" or node_status["Ready"] == "Unknown" or node_cpu_usage > 80) and not node_info[node_name]["is_migration"]:
            need_migrate_nodes.append(node_name)
            if node_status["Ready"] == "False":
                print(f"节点 {node_name} 状态异常: Not Ready")
                logging.info(f"节点 {node_name} 状态异常: Not Ready")
                r.publish("node_status", json.dumps({
                    "timestamp": time.strftime("%Y%m%d-%H%M%S", time.localtime()),
                    "node_name": node_name,
                    "status": "Not Ready"
                }))
            elif node_status["Ready"] == "Unknown":
                print(f"节点 {node_name} 状态未知: Unknown")
                logging.info(f"节点 {node_name} 状态未知: Unknown")
                r.publish("node_status", json.dumps({
                    "timestamp": time.strftime("%Y%m%d-%H%M%S", time.localtime()),
                    "node_name": node_name,
                    "status": "Unknown"
                }))
            else:
                logging.info(f"节点 {node_name} CPU 负载过高: {node_cpu_usage}%")
                print(f"节点 {node_name} CPU 负载过高: {node_cpu_usage}%")
                r.publish("node_status", json.dumps({
                    "timestamp": time.strftime("%Y%m%d-%H%M%S", time.localtime()),
                    "node_name": node_name,
                    "status": "High CPU Load"
                }))

            # 将状态发至前端

        if node_status["Ready"] == "True":
            node_is_ready.append(node_name)
            node_cpu_usage_list.append([node_name, node_cpu_usage])

    # 节点负载过高/故障状态下迁移 Pod
    for node in need_migrate_nodes:
        node_data = node_info[node]
        sorted_node_cpu_usage = sorted(node_cpu_usage_list, key=lambda x: x[1])
        target_node_name = sorted_node_cpu_usage[0][0]
        if target_node_name == 'ec3':
            target_node_name = sorted_node_cpu_usage[1][0]  # 如果目标节点是 ec3，则选择第二个节点
        if not node_data["is_migration"]:
            print(f"节点 {node} 需要迁移 Pod")
            logging.info(f"节点 {node} 需要迁移 Pod")
            target_node_selector = {node_info[target_node_name]["label_key"]: node_info[target_node_name]["label_value"]}
            # new_cpu_limit = "1"
            pods = v1.list_namespaced_pod(
                namespace="default",
                field_selector=f"spec.nodeName={node}"
            )
            for pod in pods.items:
                if "processor-service" in pod.metadata.name:
                    pod_name_uid = pod.metadata.name
                    pod_name = pod_name_uid.split("-")[:-2]
                    pod_name = "-".join(pod_name)
                    migrate_pod(apps_v1, node, pod, target_node_selector)
                    node_info[node]["is_migration"] = True
                    node_info[node]["target_node"] = target_node_name
                    node_info[node]["migration_pod"].append(pod_name)
                    print(f"Pod {pod.metadata.name} 已迁移到 {target_node_selector}")
                    logging.info(f"Pod {pod.metadata.name} 已迁移到 {target_node_selector}")

            pods_monitoring = v1.list_namespaced_pod(
                namespace="monitoring",
                field_selector=f"spec.nodeName={node}"
            )

            for pod in pods_monitoring.items:
                if "prometheus" in pod.metadata.name:
                    pod_name_uid = pod.metadata.name
                    pod_name = pod_name_uid.split("-")[:-2]
                    pod_name = "-".join(pod_name)
                    migrate_pod(apps_v1, node, pod, target_node_selector)
                    node_info[node]["is_migration"] = True
                    node_info[node]["target_node"] = target_node_name
                    node_info[node]["migration_pod"].append(pod_name)
                    print(f"Pod {pod.metadata.name} 已迁移到 {target_node_selector}")
                    logging.info(f"Pod {pod.metadata.name} 已迁移到 {target_node_selector}")

def redis_listener():
    r = redis.StrictRedis(host=redis_host, port=redis_port, decode_responses=True)
    pubsub = r.pubsub()
    pubsub.subscribe(CHANNEL)
    print(f"[监听线程] 正在监听 Redis 频道：{CHANNEL}")

    for message in pubsub.listen():
        if message["type"] == "message":
            print(f"频道[{CHANNEL}] 收到消息：{message['data']}")
            logging.info(f"频道[{CHANNEL}] 收到消息：{message['data']}")
            data = json.loads(message['data'])
            pod_name = data['pod_name']
            namespace = data['namespace']
            delete_malicious_pod_and_node(pod_name, namespace)

    
if __name__ == "__main__":
    listener_thread = threading.Thread(target=redis_listener, daemon=True)
    listener_thread.start()
    print("[主线程] Redis 监听线程已启动")

    try:
        # 节点初始化
        node_info = initialize_node_info()
        logging.info("节点信息初始化完成")
        pod_restart_counts = {}
        while True:
            schedule_policy(node_info, pod_restart_counts)
            time.sleep(2)  # 每分钟执行一次调度策略
    except KeyboardInterrupt:
        print("[主线程] 接收到中断，程序退出")


