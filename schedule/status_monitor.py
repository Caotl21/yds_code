import os
import redis
import time
import json
import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException

PROMETHEUS_URL = "http://10.244.241.72:30090"  # 使用你的节点IP和NodePort

def query_prometheus(query):
    """查询Prometheus指标"""
    try:
        response = requests.get(f"{PROMETHEUS_URL}/api/v1/query", 
                              params={'query': query}, timeout=10)
        print(f"查询: {query}")
        print(f"响应状态: {response.status_code}")
        if response.status_code == 200:
            result = response.json()
            print(f"返回数据: {result}")
            return result['data']['result']
        else:
            print(f"查询失败: {response.text}")
        return []
    except Exception as e:
        print(f"查询Prometheus失败: {e}")
        return []

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

        return -1
    except Exception as e:
        print(f"获取指标失败: {e}")
        # logging.error(f"获取指标失败: {e}")
        return -1
    
def get_node_cpu_usage_prometheus(prometheus_url, node_ip):
    query = '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5s])) * 100)'
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
    
def query_prometheus(query):
    """查询Prometheus指标"""
    try:
        response = requests.get(f"{PROMETHEUS_URL}/api/v1/query", 
                              params={'query': query}, timeout=10)
        # print(f"查询: {query}")
        # print(f"响应状态: {response.status_code}")
        if response.status_code == 200:
            result = response.json()
            # print(f"返回数据: {result}")
            return result['data']['result']
        else:
            print(f"查询失败: {response.text}")
        return []
    except Exception as e:
        print(f"查询Prometheus失败: {e}")
        return []

def get_nodes():
    config.load_kube_config()
    v1 = client.CoreV1Api()
    try:
        nodes = v1.list_node().items
        return [node.metadata.name for node in nodes]
    except ApiException as e:
        print(f"获取节点列表失败: {e}")
        # logging.error(f"获取节点列表失败: {e}")
        return []

def get_node_name(node):
    """获取节点名称"""
    return node.metadata.name if hasattr(node, 'metadata') and hasattr(node.metadata, 'name') else None

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
        # logging.error(f"获取节点状态失败: {e}")
    return status

def get_node_ip(node):
    """获取节点IP地址"""
    if hasattr(node, 'status') and hasattr(node.status, 'addresses'):
        for address in node.status.addresses:
            if address.type == "InternalIP":
                return address.address
    return None



while True:
    config.load_kube_config()
    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    nodes = v1.list_node()
    for node in nodes.items:
        node_name = get_node_name(node)
        node_ip = get_node_ip(node)
        node_status = get_node_status(node)
        cpu_usage = get_node_cpu_usage(node_name)
        cpu_usage_prometheus = get_node_cpu_usage_prometheus(PROMETHEUS_URL, node_ip)
        cpu_query = '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5s])) * 100)'
        # cpu_results = query_prometheus(cpu_query)
        # 内存使用率查询
        memory_query = '(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100'
        memory_results = query_prometheus(memory_query)
        
        # 网络流量查询 - 只取主要网络接口
        network_query = 'sum by (instance) (rate(node_network_receive_bytes_total{device!~"lo|docker.*|cali.*|veth.*|tunl.*"}[5s]))'
        network_results = query_prometheus(network_query)
        
        for result in network_results:
            instance = result['metric'].get('instance', '')
            ip = instance.split(':')[0] if ':' in instance else instance
            
            if ip == node_ip:
                # 只处理当前节点的网络数据
                network_usage = float(result['value'][1])/1024

                print(f"网络: {instance} -> {node_name} = {network_usage:.1f} bytes/s")
                break

        print(f"内存查询结果数量: {len(memory_results)}")
        print(f"网络查询结果数量: {len(network_results)}")
        print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Node {node_name} 状态: {node_status}")
        print(f"Node {node_name} CPU 使用率 (K8s): {cpu_usage:.2f}%")
        print(f"Node {node_name} CPU 使用率 (Prometheus): {cpu_usage_prometheus:.2f}%")
        # print(f"Node {node_name} CPU 使用率 (Prometheus 查询): {cpu_results}")

    time.sleep(5)  # 每5秒更新一次节点状态