import redis
from kubernetes import client, config
import time
import os
import json
from datetime import datetime, timedelta, timezone

# 连接 Redis
redis_host = os.environ.get("REDIS_HOST", "10.244.231.134")
redis_port = int(os.environ.get("REDIS_PORT", 6379))
r = redis.Redis(host=redis_host, port=redis_port)

# 加载 K8s 配置
try:
    # 尝试加载集群内配置（在 Pod 中运行时）
    config.load_incluster_config()
    print("使用集群内配置")
except Exception as e:
    # 如果失败，则尝试加载本地 kubeconfig 文件
    try:
        config.load_kube_config()
        print("使用本地 kubeconfig 配置")
    except Exception as e:
        print(f"无法加载 Kubernetes 配置: {e}")
        raise

v1 = client.CoreV1Api()
metrics_api = client.CustomObjectsApi()

def get_node_metrics():
    """获取节点的 CPU 和内存使用率"""
    try:
        metrics = metrics_api.list_cluster_custom_object(
            "metrics.k8s.io", "v1beta1", "nodes"
        )
        node_metrics = {}
        for item in metrics["items"]:
            node_name = item["metadata"]["name"]
            # 解析 CPU 使用率
            cpu_usage = item["usage"]["cpu"]
            if cpu_usage.endswith("n"):
                cpu_usage = int(cpu_usage[:-1]) / 1000000000  # 转换纳核为核
            elif cpu_usage.endswith("m"):
                cpu_usage = int(cpu_usage[:-1]) / 1000  # 转换毫核为核
            else:
                cpu_usage = float(cpu_usage)
                
            # 解析内存使用率
            memory_usage = item["usage"]["memory"]
            if memory_usage.endswith("Ki"):
                memory_usage = int(memory_usage[:-2]) * 1024
            elif memory_usage.endswith("Mi"):
                memory_usage = int(memory_usage[:-2]) * 1024 * 1024
            elif memory_usage.endswith("Gi"):
                memory_usage = int(memory_usage[:-2]) * 1024 * 1024 * 1024
            else:
                memory_usage = int(memory_usage)
                
            node_metrics[node_name] = {
                "cpu_usage": cpu_usage,
                "memory_usage": memory_usage
            }
        return node_metrics
    except Exception as e:
        print(f"获取节点指标失败: {e}")
        return {}

def calculate_uptime(start_time):
    """计算运行时间并格式化为人类可读格式"""
    if not start_time:
        return "未知"
    
    # 确保时间对象有时区信息
    if start_time.tzinfo is None:
        # 如果没有时区信息，假设是UTC时间
        start_time = start_time.replace(tzinfo=timezone.utc)
    
    # 使用带时区的当前时间
    now = datetime.now(timezone.utc)
    uptime = now - start_time
    
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m {seconds}s"

def collect_k8s_metrics():
    """收集 K8s 节点和 Pod 的指标"""
    try:
        # 1. 获取节点状态和资源
        nodes = v1.list_node().items
        
        # 尝试获取节点指标，如果失败则使用空字典
        try:
            node_metrics = get_node_metrics()
        except Exception as e:
            print(f"获取节点指标失败，将使用默认值: {e}")
            node_metrics = {}
        
        for node in nodes:
            node_name = node.metadata.name
            
            # 获取节点状态
            node_status = "Unknown"
            for condition in node.status.conditions:
                if condition.type == "Ready":
                    node_status = "Ready" if condition.status == "True" else "NotReady"
                    break
            
            # 计算节点运行时间
            creation_timestamp = node.metadata.creation_timestamp
            uptime = calculate_uptime(creation_timestamp)
            
            # 获取节点 CPU 和内存总量
            cpu_capacity = node.status.capacity.get("cpu", "0")
            memory_capacity = node.status.capacity.get("memory", "0")
            
            # 计算 CPU 和内存使用率
            metrics = node_metrics.get(node_name, {})
            cpu_usage = metrics.get("cpu_usage", 0)
            memory_usage = metrics.get("memory_usage", 0)
            
            # 转换内存容量为字节
            if memory_capacity.endswith("Ki"):
                memory_capacity_bytes = int(memory_capacity[:-2]) * 1024
            elif memory_capacity.endswith("Mi"):
                memory_capacity_bytes = int(memory_capacity[:-2]) * 1024 * 1024
            elif memory_capacity.endswith("Gi"):
                memory_capacity_bytes = int(memory_capacity[:-2]) * 1024 * 1024 * 1024
            else:
                memory_capacity_bytes = int(memory_capacity)
            
            # 计算使用率百分比
            cpu_usage_percent = (cpu_usage / float(cpu_capacity)) * 100 if cpu_capacity != "0" else 0
            memory_usage_percent = (memory_usage / memory_capacity_bytes) * 100 if memory_capacity_bytes > 0 else 0
            
            node_data = {
                "name": node_name,
                "status": node_status,
                "cpu_usage": f"{cpu_usage_percent:.1f}",
                "memory_usage": f"{memory_usage_percent:.1f}",
                "uptime": uptime
            }
            
            # 发布到 node_info 频道（JSON 格式）
            r.publish("node_info", json.dumps(node_data))
            
            # 保留原有的 Hash 存储
            r.hset(f"k8s:node:{node_name}", mapping=node_data)
            
            print(f"节点 {node_name}: 状态={node_status}, CPU={cpu_usage_percent:.1f}%, 内存={memory_usage_percent:.1f}%, 运行时间={uptime}")

        # 2. 获取所有 Pod 状态
        pods = v1.list_pod_for_all_namespaces().items
        for pod in pods:
            pod_name = pod.metadata.name
            namespace = pod.metadata.namespace
            node_name = pod.spec.node_name or "未分配"
            phase = pod.status.phase
            
            # 计算 Pod 运行时间
            creation_timestamp = pod.metadata.creation_timestamp
            uptime = calculate_uptime(creation_timestamp)
            
            # 获取容器状态
            container_statuses = []
            if pod.status.container_statuses:
                for container in pod.status.container_statuses:
                    container_name = container.name
                    container_ready = container.ready
                    container_status = "Unknown"
                    restart_count = container.restart_count
                    
                    if hasattr(container.state, 'running') and container.state.running:
                        container_status = "Running"
                        start_time = container.state.running.started_at
                        container_uptime = calculate_uptime(start_time)
                    elif hasattr(container.state, 'waiting') and container.state.waiting:
                        container_status = f"Waiting: {container.state.waiting.reason}"
                    elif hasattr(container.state, 'terminated') and container.state.terminated:
                        container_status = f"Terminated: {container.state.terminated.reason}"
                    
                    container_statuses.append({
                        "name": container_name,
                        "status": container_status,
                        "ready": str(container_ready),
                        "restarts": str(restart_count)
                    })
            #我想写如果namespace不是kube-system则加入字典
            if namespace != "kube-system":
                pod_data = {
                    "name": pod_name,
                    "namespace": namespace,
                    "status": phase,
                    "node": node_name,
                    "uptime": uptime
                }
                r.publish("pod_info", json.dumps(pod_data))
                r.hset(f"k8s:pod:{namespace}:{pod_name}", mapping={
                    **pod_data # 保持与原脚本兼容
                })
                print(f"Pod {namespace}/{pod_name}: 状态={phase}, 节点={node_name}, 运行时间={uptime}")
            
    except Exception as e:
        print(f"收集 K8s 指标失败: {e}")

# 主循环
def main():
    print(f"K8s 数据收集器已启动，连接到 Redis: {redis_host}:{redis_port}")
    while True:
        try:
            collect_k8s_metrics()
        except Exception as e:
            print(f"收集周期出错: {e}")
        
        # 每 30 秒采集一次
        time.sleep(1)

if __name__ == "__main__":
    main()
