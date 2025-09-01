import requests
import json
import redis
import time
from datetime import datetime
from kubernetes import client, config
import os

# 加载K8s配置
try:
    config.load_incluster_config()
except:
    config.load_kube_config()

v1 = client.CoreV1Api()

# Prometheus配置 - 使用NodePort访问
# 使用Service域名
#PROMETHEUS_URL = "http://192.168.1.120:30090"
#PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://192.168.1.120:30090")  # 使用你的节点IP和NodePort
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://10.111.12.181:9090") 

def setup_redis():
    """设置Redis连接"""
    redis_host = os.getenv("REDIS_HOST", "10.96.252.224")
    redis_port = int(os.getenv("REDIS_PORT", 6379))
    
    max_retries = 10
    retry_delay = 2
    
    for retry in range(max_retries):
        try:
            r = redis.Redis(
                host=redis_host,
                port=redis_port,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
            # 测试连接
            r.ping()
            print(f"成功连接到 Redis: {redis_host}:{redis_port}")
            return r
        except Exception as e:
            if retry < max_retries - 1:
                print(f"Redis连接失败 (尝试 {retry+1}/{max_retries}): {e}")
                print(f"{retry_delay} 秒后重试...")
                time.sleep(retry_delay)
            else:
                print(f"达到最大重试次数 ({max_retries})，无法连接到 Redis")
                raise
    return None, None


def query_prometheus(query):
    """查询Prometheus指标"""
    try:
        response = requests.get(f"{PROMETHEUS_URL}/api/v1/query", 
                              params={'query': query}, timeout=10)
        #print(f"查询: {query}")
        #print(f"响应状态: {response.status_code}")
        if response.status_code == 200:
            result = response.json()
            #print(f"返回数据: {result}")
            return result['data']['result']
        else:
            print(f"查询失败: {response.text}")
        return []
    except Exception as e:
        print(f"查询Prometheus失败: {e}")
        return []

def calculate_uptime(creation_timestamp):
    """计算运行时间"""
    if not creation_timestamp:
        return "未知"
    
    now = datetime.now(creation_timestamp.tzinfo)
    uptime = now - creation_timestamp
    
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    if days > 0:
        return f"{days}天{hours}小时{minutes}分钟"
    elif hours > 0:
        return f"{hours}小时{minutes}分钟"
    else:
        return f"{minutes}分钟{seconds}秒"

def collect_node_metrics():
    """从Prometheus收集节点指标"""
    # 先获取K8s节点信息，建立IP到节点名的映射
    nodes = v1.list_node().items
    ip_to_node = {}
    
    for node in nodes:
        # 获取节点的内部IP
        for address in node.status.addresses:
            if address.type == "InternalIP":
                ip_to_node[address.address] = node.metadata.name
                break
    
    #print(f"IP到节点名映射: {ip_to_node}")
    
    # CPU使用率查询
    #cpu_query = '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[10s])) * 100)'
    cpu_query = '(1 - avg(rate(node_cpu_seconds_total{mode="idle"}[10s])) by (instance)) *100'
    cpu_results = query_prometheus(cpu_query)
    
    # 内存使用率查询
    #memory_query = '(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100'
    memory_query = '(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100'
    memory_results = query_prometheus(memory_query)
    
    # 网络流量查询 - 只取主要网络接口
    network_query = 'sum by (instance) (rate(node_network_receive_bytes_total{device!~"lo|docker.*|cali.*|veth.*|tunl.*"}[30s]))'
    network_results = query_prometheus(network_query)
    
    node_metrics = {}
    
    print(f"CPU查询结果数量: {len(cpu_results)}")
    print(f"内存查询结果数量: {len(memory_results)}")
    print(f"网络查询结果数量: {len(network_results)}")
    
    # 处理CPU数据
    for result in cpu_results:
        instance = result['metric'].get('instance', '')
        ip = instance.split(':')[0] if ':' in instance else instance
        node_name = ip_to_node.get(ip, ip)  # 如果找不到映射就用IP
        
        cpu_usage = float(result['value'][1])
        
        if node_name not in node_metrics:
            node_metrics[node_name] = {}
        node_metrics[node_name]['cpu_usage'] = cpu_usage
        #print(f"CPU: {instance} -> {node_name} = {cpu_usage:.1f}%")
    
    # 处理内存数据
    for result in memory_results:
        instance = result['metric'].get('instance', '')
        ip = instance.split(':')[0] if ':' in instance else instance
        node_name = ip_to_node.get(ip, ip)
        
        memory_usage = float(result['value'][1])
        
        if node_name not in node_metrics:
            node_metrics[node_name] = {}
        node_metrics[node_name]['memory_usage'] = memory_usage
        #print(f"内存: {instance} -> {node_name} = {memory_usage:.1f}%")

    # 处理网络数据
    for result in network_results:
        instance = result['metric'].get('instance', '')
        ip = instance.split(':')[0] if ':' in instance else instance
        node_name = ip_to_node.get(ip, ip)
        
        network_usage = float(result['value'][1])
        
        if node_name not in node_metrics:
            node_metrics[node_name] = {}
        node_metrics[node_name]['network_usage'] = network_usage
        #print(f"网络: {instance} -> {node_name} = {network_usage:.1f} bytes/s")
    
    #print(f"最终节点指标: {node_metrics}")
    return node_metrics

def collect_pod_metrics():
    """从Prometheus收集Pod网络指标"""
    # 使用rate函数计算网络流量速率，并按pod和namespace聚合
    # PromQL (Prometheus Query Language) 查询语句，用于从 Prometheus 监控系统中获取数据
    pod_network_rx_query = 'sum by (pod, namespace) (rate(container_network_receive_bytes_total{pod!=""}[1m]))'
    pod_rx_results = query_prometheus(pod_network_rx_query)
    
    pod_network_tx_query = 'sum by (pod, namespace) (rate(container_network_transmit_bytes_total{pod!=""}[1m]))'
    pod_tx_results = query_prometheus(pod_network_tx_query)
    
    print(f"Pod RX查询结果: {len(pod_rx_results)} 个Pod")
    print(f"Pod TX查询结果: {len(pod_tx_results)} 个Pod")
    
    pod_metrics = {}
    
    # 处理接收流量
    for result in pod_rx_results:
        pod_name = result['metric'].get('pod', '')
        namespace = result['metric'].get('namespace', '')
        rx_rate = float(result['value'][1])  # bytes/second
        
        if pod_name and namespace:
            key = f"{namespace}/{pod_name}"
            if key not in pod_metrics:
                pod_metrics[key] = {}
            pod_metrics[key]['network_rx'] = rx_rate
    
    # 处理发送流量
    for result in pod_tx_results:
        pod_name = result['metric'].get('pod', '')
        namespace = result['metric'].get('namespace', '')
        tx_rate = float(result['value'][1])  # bytes/second
        
        if pod_name and namespace:
            key = f"{namespace}/{pod_name}"
            if key not in pod_metrics:
                pod_metrics[key] = {}
            pod_metrics[key]['network_tx'] = tx_rate
    
    '''print(f"\n=== Pod网络流量统计 ===")
    for pod_key, metrics in pod_metrics.items():
        rx_kb = metrics.get('network_rx', 0) / 1024
        tx_kb = metrics.get('network_tx', 0) / 1024
        print(f"{pod_key}: RX={rx_kb:.2f}KB/s, TX={tx_kb:.2f}KB/s")'''
    
    return pod_metrics

def detect_network_attacks(pod_metrics):
    """检测网络攻击模式"""
    alerts = []
    
    # 检测网络洪水攻击
    for pod_key, metrics in pod_metrics.items():
        rx_rate = metrics.get('network_rx', 0)
        tx_rate = metrics.get('network_tx', 0)
        
        # 检测异常高的出站流量（攻击特征）
        if tx_rate > 50 * 1024 * 1024:  # 50MB/s出站
            alerts.append({
                'type': 'NETWORK_FLOOD_ATTACK',
                'pod': pod_key,
                'tx_rate': f"{tx_rate/1024/1024:.2f}MB/s",
                'severity': 'critical'
            })
        
        # 检测连接数异常
        connection_ratio = tx_rate / max(rx_rate, 1)
        if connection_ratio > 50:  # 出站远大于入站
            alerts.append({
                'type': 'SUSPICIOUS_OUTBOUND_TRAFFIC',
                'pod': pod_key,
                'ratio': f"{connection_ratio:.1f}:1",
                'severity': 'warning'
            })
    
    return alerts

def check_prometheus_targets():
    """检查Prometheus的targets状态"""
    try:
        response = requests.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=10)
        if response.status_code == 200:
            targets = response.json()['data']['activeTargets']
            print("=== Prometheus Targets ===")
            for target in targets:
                job = target.get('labels', {}).get('job', 'unknown')
                health = target.get('health', 'unknown')
                endpoint = target.get('scrapeUrl', 'unknown')
                print(f"Job: {job}, Health: {health}, Endpoint: {endpoint}")
        else:
            print(f"无法获取targets: {response.text}")
    except Exception as e:
        print(f"检查targets失败: {e}")

def debug_container_metrics():
    """调试容器指标可用性"""
    print("=== 检查Prometheus配置 ===")
    check_prometheus_targets()
    
    # 检查基础容器指标
    basic_queries = [
        'up',
        'container_network_receive_bytes_total',
        'container_network_transmit_bytes_total'
    ]
    
    for query in basic_queries:
        try:
            results = query_prometheus(query)
            print(f"✓ {query}: {len(results)} 个结果")
            if results and 'container_network' in query:
                sample = results[0]
                print(f"  示例标签: {sample.get('metric', {})}")
        except Exception as e:
            print(f"✗ {query}: 查询失败 - {e}")

def collect_and_publish(redis_client):
    """收集并发布指标到Redis"""
    #r = redis.Redis(host='10.244.231.131', port=6379, decode_responses=True)
    
    while True:
        try:
            # 获取Prometheus节点指标
            node_metrics = collect_node_metrics()
            
            # 获取Pod网络指标
            pod_metrics = collect_pod_metrics()
            
            # 检测网络攻击
            # alerts = detect_network_attacks(pod_metrics)
            # if alerts:
            #     for alert in alerts:
            #         print(f"⚠️  网络攻击警告: {alert}")
            #         redis_client.publish("security_alerts", json.dumps(alert))
            
            # 获取K8s节点状态
            nodes = v1.list_node().items
            
            for node in nodes:
                # 获取节点信息
                architecture = node.status.node_info.architecture
                os_image = node.status.node_info.os_image
                kernel_version = node.status.node_info.kernel_version
                node_name = node.metadata.name
                # 指令集:
                
                # 获取节点状态
                node_status = "Unknown"
                for condition in node.status.conditions:
                    if condition.type == "Ready":
                        node_status = "Ready" if condition.status == "True" else "NotReady"
                        break
                
                # 从Prometheus获取实时指标
                metrics = node_metrics.get(node_name, {})
                cpu_usage = metrics.get('cpu_usage', 0)
                memory_usage = metrics.get('memory_usage', 0)
                network_usage = metrics.get('network_usage', 0) / 1024
                
                node_data = {
                    "name": node_name,
                    "architecture": architecture,
                    "os_image": os_image,
                    "kernel_version": kernel_version,
                    "status": node_status,
                    "cpu_usage": f"{cpu_usage:.2f}",
                    "memory_usage": f"{memory_usage:.2f}",
                    "network_usage": f"{network_usage:.2f}KB/s",
                    "uptime": calculate_uptime(node.metadata.creation_timestamp),
                    "timestamp": datetime.now().isoformat()
                }
                
                # 发布到Redis
                redis_client.publish("node_info", json.dumps(node_data))
                #print(f"节点 {node_name}: 状态={node_status} ，CPU={cpu_usage:.2f}%, 内存={memory_usage:.2f}%， 网络流量={network_usage:.2f} KB/s ，运行时间={node_data['uptime']},"
                      #f"节点架构={architecture}，操作系统={os_image}，内核版本={kernel_version}")
            
            # 获取K8s Pod状态
            pods = v1.list_pod_for_all_namespaces().items
            
            for pod in pods:
                pod_name = pod.metadata.name
                namespace = pod.metadata.namespace
                node_name = pod.spec.node_name or "未分配"
                phase = pod.status.phase
                restart_count = pod.status.container_statuses[0].restart_count if pod.status.container_statuses else 0
                pod_key = f"{namespace}/{pod_name}"
                
                # 计算Pod运行时间
                creation_timestamp = pod.metadata.creation_timestamp
                uptime = calculate_uptime(creation_timestamp)

                if pod_key in pod_metrics:
                    metrics = pod_metrics[pod_key]
                    if namespace != "kube-system":
                        pod_data = {
                            "name": pod_name,
                            "namespace": namespace,
                            "status": phase,
                            "node": node_name,
                            "uptime": uptime,
                            "restart_count": restart_count,
                            "timestamp": datetime.now().isoformat(),
                            "network_rx_rate": f"{metrics.get('network_rx', 0)/1024:.2f}KB/s",
                            "network_tx_rate": f"{metrics.get('network_tx', 0)/1024:.2f}KB/s",
                        }

                        redis_client.publish("pod_info", json.dumps(pod_data))
                        #print(f"Pod {namespace}/{pod_name}: 状态={phase}, 节点={node_name}, 工作时间={uptime}, 重启次数={restart_count}, "
                              #f"接收流量={metrics.get('network_rx', 0)/1024:.2f}KB/s, "
                              #f"发送流量={metrics.get('network_tx', 0)/1024:.2f}KB/s")
                #当没检测到网络流量时 也发送其他的信息 网络流量为上次发送的流量值
                else:
                    if namespace != "kube-system":
                        pod_data = {
                            "name": pod_name,
                            "namespace": namespace,
                            "status": phase,
                            "node": node_name,
                            "uptime": uptime,
                            "restart_count": restart_count,
                            "timestamp": datetime.now().isoformat(),
                            'network_rx_rate': "0KB/s",
                            'network_tx_rate': "0KB/s",
                        }

                        redis_client.publish("pod_info", json.dumps(pod_data))
                        #print(f"Pod {namespace}/{pod_name}: 状态={phase}, 节点={node_name}, 工作时间={uptime}, 重启次数={restart_count}, "
                              #f"接收流量=0KB/s, "
                              #f"发送流量=0KB/s")        


        except Exception as e:
            print(f"收集指标失败: {e}")
        
        # 每1秒更新一次
        time.sleep(1)

if __name__ == "__main__":
    redis_client = setup_redis()
    collect_and_publish(redis_client)
