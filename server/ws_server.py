# main.py
import redis
import asyncio
import json
import os
import queue
import time
import threading
import paramiko
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import collections
import platform
import socket
import psutil
import subprocess
import base64
from datetime import datetime

current_time = datetime.now().strftime('%Y%m%d')
last_belt1_time = time.time()
last_belt2_time = time.time()
logger_server = logging.getLogger('server')
logger_server.setLevel(logging.INFO)
logger_server.addHandler(logging.FileHandler(f'ws-server-{current_time}.log'))
# logger_server 格式
logger_server_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
logger_server.handlers[0].setFormatter(logger_server_formatter)
logger_server.info('Server start: ws-server!')
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename=f'ws-server-{current_time}.log')
logger_attack = logging.getLogger('attack')
logger_attack.setLevel(logging.INFO)
logger_attack.addHandler(logging.FileHandler(f'attack-{current_time}.log'))
# logger_attack 格式
logger_attack_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
logger_attack.handlers[0].setFormatter(logger_attack_formatter)
logger_attack.info('Server start: attack!')


logger_detection = logging.getLogger('detection')
logger_detection.setLevel(logging.INFO)
logger_detection.addHandler(logging.FileHandler(f'detection-{current_time}.log'))
# logger_detection 格式
logger_detection_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
logger_detection.handlers[0].setFormatter(logger_detection_formatter)
logger_detection.info('Server start: detect!')
# 创建 FastAPI 应用
app = FastAPI()

# 挂载静态文件目录
app.mount("/static", StaticFiles(directory="static"), name="static")

# Redis 配置
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_CHANNELS = ['video_channel_1', 'video_channel_2', 'pod_info', 'node_info','monitor_usb_1','monitor_usb_2','node_status','security_alerts']

# 添加 K8s 信息存储
k8s_info = {
    "nodes": {},
    "pods": {},
    "last_updated": None
}

attack_info = {
    "node_name":{},
    "status":{}
}
attack_data = {}


# 全局变量
redis_client = None
redis_pubsub = None
redis_thread = None
redis_connected = False

# 系统启动时间
start_time = time.time()

# 添加回原有的全局变量
#redis_host1 = os.getenv("REDIS_HOST_1", "localhost")
#redis_port1 = int(os.getenv("REDIS_PORT_1", 6379))
arm_host = os.getenv("ARM_HOST", "192.168.1.116")
#r1 = redis.Redis(host=redis_host1, port=redis_port1, db=0)
task_queue = queue.Queue()
detection_results = {
    "belt1": {
        "status": "waiting", 
        "detect": 0, 
        "timestamp": None, 
        "class": "",
        "camera_id": "unknown",
        "model_name": "unknown",
        "detections": [],
        "image": None,
        "perf_info": {}
    },
    "belt2": {
        "status": "waiting", 
        "detect": 0, 
        "timestamp": None, 
        "class": "",
        "camera_id": "unknown",
        "model_name": "unknown",
        "detections": [],
        "image": None,
        "perf_info": {}
    }
}

# 添加帧缓存
frame_cache = {
    "belt1": {"v5lite_e_int8": collections.deque(maxlen=5)},
    "belt2": {"v5lite_e_int8": collections.deque(maxlen=5)}
}

# WebSocket 连接管理器
class ConnectionManager:
    def __init__(self):
        self.active_connections = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                print(f"[WebSocket] 广播消息出错: {e}")

# 初始化 Redis 连接
def init_redis():
    global redis_client, redis_pubsub, redis_connected
    try:
        redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)
        redis_pubsub = redis_client.pubsub()
        redis_pubsub.subscribe(*REDIS_CHANNELS)
        redis_connected = True
        print(f"[Redis] 连接成功: {REDIS_HOST}:{REDIS_PORT}")
        print(f"[Redis] 开始监听 Redis 频道: {', '.join(REDIS_CHANNELS)}")
        return True
    except Exception as e:
        print(f"[Redis] 连接失败: {e}")
        redis_connected = False
        return False

# 处理 Redis 消息
def handle_redis_message(message):
    if message['type'] != 'message':
        return
    
    channel = message['channel'].decode('utf-8')
    try:
        data = json.loads(message['data'].decode('utf-8'))
        
        # 处理 K8s 节点信息
        if channel == 'node_info':
            node_name = data.get('name', 'unknown')
            # 存储时使用完整的节点名称作为key
            k8s_info['nodes'][node_name] = {
                'status': data.get('status', 'Unknown'),
                'cpu_percent': float(data.get('cpu_usage', '0.0')),  # 注意字段名变化
                'memory_percent': float(data.get('memory_usage', '0.0')),  # 注意字段名变化
                'network_usage': data.get('network_usage', '0.0'),  # 注意字段名变化
                'architecture': data.get('architecture', '未知'),
                'os_image': data.get('os_image', '未知'),
                'kernel_version': data.get('kernel_version', '未知'),
                'uptime': data.get('uptime', '未知'),
            }
            k8s_info['last_updated'] = datetime.now().isoformat()
            print(f"[Redis] 更新节点信息: {node_name}")
            

            
        # 处理 K8s Pod 信息
        elif channel == 'pod_info':
            pod_name = data.get('name', 'unknown')
            namespace = data.get('namespace', 'default')
            pod_key = f"{namespace}/{pod_name}"
            k8s_info['pods'][pod_name] = {
                'name': pod_name,
                'namespace': namespace,
                'status': data.get('status', 'Unknown'),
                'node': data.get('node', '未分配'),
                'uptime': data.get('uptime', '未知'),
                'restart_count': data.get('restart_count', 0),
                'network_rx_rate': data.get('network_rx_rate', '0KB/s'),
                'network_tx_rate': data.get('network_tx_rate', '0KB/s'),
            }
            k8s_info['last_updated'] = datetime.now().isoformat()
            print(f"[Redis] 更新Pod信息: {pod_key}")

        # 处理监控信息
        elif channel == 'monitor_usb_1':
            print(f"[Redis] 接收到来自 {channel} 的监控数据")
        
        elif channel == 'monitor_usb_2':
            print(f"[Redis] 接收到来自 {channel} 的监控数据")
        
        elif channel == 'security_alerts':
            pod_name = data.get('pod_name','unknown')
            namespace = data.get('namespace','default')
            alert = data.get('alert','unknown')
            if alert.startswith('Container'):
                attack_info.update({
                    'timestamp': datetime.now().isoformat(),
                    'node_name': pod_name,
                    'status': 'Attack Isolation',
                    'attack_type': '发生容器逃离',
                    'severity': 'critical',
                    'last_updated': datetime.now().isoformat()
                })

            print(f"[Redis] 接收到来自 {channel} 的攻击检测结果{pod_name}: Attack Isolation")
            logger_attack.info(f"attack_info: {attack_info}")
            logger_attack.info(f"[{attack_info['severity']}]--攻击检测结果：{node_name}--{attack_info['attack_type']}")
            logger_detection.info(f"[Redis] 接收到攻击检测结果: 节点{node_name}遭受攻击: ")

        elif channel == 'node_status':
            node_name = data.get('node_name', 'unknown')
            status = data.get('status', '0')
            timestamp = data.get('timestamp', datetime.now().isoformat())
            
            # 状态映射
            status_mapping = {
                'Not Ready': {'type': '未知类型攻击', 'severity': 'warning'},
                'Unknown': {'type': '设备物理受损', 'severity': 'critical'},
                'High CPU Load': {'type': '设备算力耗尽', 'severity': 'warning'},
                'CrashLoopBackOff': {'type': '程序崩溃', 'severity': 'performance'},
                'Attack Isolation':{'type':'发生容器逃离','severity':'critical'},
                'Ready': {'type': '已成功恢复', 'severity': 'success'}
            }
            
            attack_type_info = status_mapping.get(status, {'type': '未知类型攻击', 'severity': 'warning'})
            
            # 更新攻击信息
            attack_info.update({
                'timestamp': timestamp,
                'node_name': node_name,
                'status': status,
                'attack_type': attack_type_info['type'],
                'severity': attack_type_info['severity'],
                'last_updated': datetime.now().isoformat()
            })
            
            print(f"[Redis] 接收到来自 {channel} 的攻击检测结果{node_name}: {status}")
            logger_attack.info(f"[{attack_info['severity']}]--攻击检测结果：{node_name}--{attack_info['attack_type']}")
            logger_detection.info(f"[Redis] 接收到攻击检测结果: 节点{node_name}遭受攻击: ")

        # 处理检测结果
        elif 'detections' in data:
            # 确定是哪个产线的数据
            belt_key = 'belt1' if channel == 'video_channel_1' else 'belt2'
            
            # 提取性能信息
            perf_info = data.get('perf_info', {})
            
            # 提取图像数据（如果有）
            image_data = None
            if 'image' in data and data['image']:
                image_data = data['image']
            
            # 更新检测结果
            detections = data.get('detections', [])
            model = perf_info.get('model_name', '')
            
            # 更新通用字段
            detection_results[belt_key].update({
                'camera_id': data.get('camera_id', 'unknown'),
                'timestamp': data.get('create_timestamp', datetime.now().isoformat()),
                'model_name': model,
                'detections': detections,
                'image': image_data,
                'perf_info': perf_info
            })
            if len(detections) > 0:
                logger_detection.info(f"[Redis] 接收到{belt_key} 的推理结果: 模型={model}, 耗时={perf_info['total_time']}, 检测到缺陷类型={detections[0].get('class', 'unknown')}，置信度={detections[0].get('confidence', 0.0)}")
            else:
                logger_detection.info(f"[Redis] 接收到{belt_key} 的推理结果: 模型={model}, 耗时={perf_info['total_time']}, 未检测到缺陷")

            # 根据模型类型更新特定字段
            if model == "v5lite_s":
                if len(detections) > 0:
                    detection_results[belt_key]["status"] = "success"
                    detection_results[belt_key]["detect"] = 1
                    detection_results[belt_key]["class"] = detections[0].get("class", "unknown")
                else:
                    detection_results[belt_key]["status"] = "success"
                    detection_results[belt_key]["detect"] = 0
                    detection_results[belt_key]["class"] = ""
            elif model == "v5lite_e_int8":
                is_defect = 1 if len(detections) > 0 else 0
                frame_cache[belt_key]["v5lite_e_int8"].append(is_defect)
                detection_results[belt_key]["status"] = "success"
                detection_results[belt_key]["detect"] = is_defect
                if is_defect:
                    detection_results[belt_key]["class"] = detections[0].get("class", "unknown")
                else:
                    if any(frame_cache[belt_key]["v5lite_e_int8"]):
                        detection_results[belt_key]["detect"] = 1
                    else:
                        detection_results[belt_key]["detect"] = 0
            
            print(f"[Redis] 接收到来自 {channel} 的检测结果: {len(detections)} 个检测")
    except Exception as e:
        print(f"[Redis] 处理消息出错: {e}")

# Redis 监听线程
def redis_listener(loop):
    """监听 Redis 的所有频道"""
    if not redis_connected or not redis_pubsub:
        print("[Redis] 未连接，无法启动监听线程")
        return
    
    print("[Redis] 启动监听线程")
    while True:
        try:
            message = redis_pubsub.get_message()
            if message and message["type"] == "message":
                channel = message.get('channel', b'unknown').decode('utf-8')
                data = message["data"].decode('utf-8')
                print(f"[Redis] 接收到来自 {channel} 的消息")
                
                # 处理消息
                handle_redis_message(message)
                
                # 广播到对应的 WebSocket
                if channel == "video_channel_1":
                    asyncio.run_coroutine_threadsafe(
                        manager_video1.broadcast(data),
                        loop
                    )
                elif channel == "video_channel_2":
                    asyncio.run_coroutine_threadsafe(
                        manager_video2.broadcast(data),
                        loop
                    )
                elif channel == "monitor_usb_1":
                    asyncio.run_coroutine_threadsafe(
                        manager_monitor1.broadcast(data),
                        loop
                    )
                elif channel == "monitor_usb_2":
                    asyncio.run_coroutine_threadsafe(
                        manager_monitor2.broadcast(data),
                        loop
                    )
                elif channel == "security_alerts":
                    attack_data = {
                        "type": "attack_detected",
                        "data": attack_info
                    }
                    logger_server.info(f"广播攻击数据{attack_data}")
                    asyncio.run_coroutine_threadsafe(
                        manager_attack.broadcast(json.dumps(attack_data)),
                        loop
                    )
                elif channel == "node_info":
                    # 广播节点信息，包装成前端期望的格式
                    nodes_data = {
                        "nodes": k8s_info['nodes'],
                        "last_updated": k8s_info['last_updated']
                    }
                    asyncio.run_coroutine_threadsafe(
                        #manager_nodes.broadcast(json.dumps(k8s_info['nodes'])),
                        manager_nodes.broadcast(json.dumps(nodes_data)),
                        loop
                    )
                elif channel == "pod_info":
                    # 广播Pod信息，包装成前端期望的格式
                    pods_data = {
                        "pods": k8s_info['pods'],
                        "last_updated": k8s_info['last_updated']
                    }
                    asyncio.run_coroutine_threadsafe(
                        #manager_pods.broadcast(json.dumps(k8s_info['pods'])),
                        manager_pods.broadcast(json.dumps(pods_data)),
                        loop
                    )
                elif channel == "node_status":
                    # 广播攻击信息
                    
                    attack_data = {
                        "type": "attack_detected",
                        "data": attack_info
                    }
                    logger_server.info(f"广播攻击数据{attack_data}")
                    asyncio.run_coroutine_threadsafe(
                        manager_attack.broadcast(json.dumps(attack_data)),
                        loop
                    )
            
        except Exception as e:
            print(f"[Redis] 监听线程出错: {e}")
            time.sleep(1)  # 出错后等待一秒再继续

# 启动 Redis 监听线程
def start_redis_listener():
    global redis_thread
    if redis_thread is None or not redis_thread.is_alive():
        redis_thread = threading.Thread(target=redis_listener, daemon=True)
        redis_thread.start()
        print("[Redis] 监听线程已启动")

# SSH客户端：连接机械臂
class PersistentSSHClient:
    def __init__(self, host="192.168.1.115", port=22, username="ubuntu", password="ubuntu", key_filename=None, timeout=5):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_filename = key_filename
        self.timeout = timeout
        self.client = None
        self.lock = threading.Lock()  # 多线程安全执行命令

        try:
            self.connect()
        except Exception as e:
            print(f"[SSH] 连接失败: {e}")

    def connect(self):
        """初始化 SSH 连接"""
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            key_filename=self.key_filename,
            timeout=self.timeout
        )
        print(f"[SSH] Connected to {self.host}")

    def execute_command(self, command):
        """执行 SSH 命令"""
        with self.lock:
            if not self.client:
                self.connect()
            stdin, stdout, stderr = self.client.exec_command(command)
            exit_status = stdout.channel.recv_exit_status()  # 等待命令执行完成
            output = stdout.read().decode('utf-8')
            error = stderr.read().decode('utf-8')
            if exit_status != 0:
                raise Exception(f"Command failed with exit status {exit_status}: {error}")
            return output.strip()
        
    def close(self):
        if self.client:
            self.client.close()
            print(f"[SSH] Disconnected from {self.host}")

# 初始化连接管理器和SSH客户端
manager_video1 = ConnectionManager()
manager_video2 = ConnectionManager()
arm_client = PersistentSSHClient(host=arm_host)
# 初始化POD和NODE信息ws连接管理器
manager_nodes = ConnectionManager()
manager_pods = ConnectionManager()
# 初始化监控画面ws连接管理器
manager_monitor1 = ConnectionManager()
manager_monitor2 = ConnectionManager()
# 初始化攻击检测ws连接管理器
manager_attack = ConnectionManager()

# SSH任务监听线程
def ssh_task_listener():
    class_dict = {
        "bruise": "0",
        "laps": "1",
        "scratches_1": "2",
        "scratches_2": "2"
    }
    while True:
        if not task_queue.empty():
            task = task_queue.get()
            belt = task.get("belt")
            belt_n = belt.split("belt")[-1]  # 获取带子编号
            if belt == "belt1":
                belt_n = "1"
            elif belt == "belt2":
                belt_n = "2"

            class_name = task.get("class")
            class_n = class_dict.get(class_name, "0")  # 默认使用 bruise 类别
            # 这里使用 paramiko 发送 SSH 命令
            try:
                # ssh
                print(f"[SSH] 发送命令: sudo python3 /home/ubuntu/Ai_FPV/YDS/yds_{belt_n}_{class_n}.py")
                arm_client.execute_command(f"sudo python3 /home/ubuntu/Ai_FPV/YDS/yds_{belt_n}_{class_n}.py")
            except Exception as e:
                print(f"[SSH] 执行失败: {e}")
        time.sleep(0.1)  # 每秒检查一次任务队列

# 路由：主页
@app.get("/")
async def root():
    return {"message": "WebSocket Server API"}

# 路由：仪表盘页面
@app.get("/dashboard")
async def dashboard():
    return FileResponse("static/dashboard.html")

# API：获取检测结果
@app.get("/api/detection_results")
async def get_detection_results():
    return detection_results

# WebSocket 端点
@app.websocket("/ws/video1")
async def websocket_video1(websocket: WebSocket):
    await manager_video1.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception as e:
        print(f"[video1] WebSocket error: {e}")
    finally:
        manager_video1.disconnect(websocket)

@app.websocket("/ws/video2")
async def websocket_video2(websocket: WebSocket):
    await manager_video2.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception as e:
        print(f"[video2] WebSocket error: {e}")
    finally:
        manager_video2.disconnect(websocket)

@app.websocket("/ws/monitor1")
async def websocket_monitor1(websocket: WebSocket):
    await manager_monitor1.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception as e:
        print(f"[monitor1] WebSocket error: {e}")
    finally:
        manager_monitor1.disconnect(websocket)

@app.websocket("/ws/monitor2")
async def websocket_monitor2(websocket: WebSocket):
    await manager_monitor2.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception as e:
        print(f"[monitor2] WebSocket error: {e}")
    finally:
        manager_monitor2.disconnect(websocket)

# API：获取带子1的检测结果并触发任务
@app.get("/belt1")
async def belt1():
    # result = detection_results["belt1"]
    global last_belt1_time
    belt1_time = time.time()
    if belt1_time - last_belt1_time > 0.3:
        last_belt1_time = belt1_time
        result = {k: v for k, v in detection_results["belt1"].items() if k != 'image'}
        print(f"[belt1] result: {result}")
        logger_server.info(f"[belt1] result: {result}")
        if result["detect"]:
            task_queue.put({
                "belt": "belt1",
                "class": result["class"]
            })
        return result
    else:
        last_belt1_time = belt1_time
        return {"status": "waiting", 
                "detect": 0, 
                "timestamp": None, 
                "class": ""}

# API：获取带子2的检测结果并触发任务
@app.get("/belt2")
async def belt2():
    global last_belt2_time

    belt2_time = time.time()
    if belt2_time - last_belt2_time > 0.3:
        last_belt2_time = belt2_time
        result = {k: v for k, v in detection_results["belt2"].items() if k != 'image'}
        print(f"[belt2] result: {result}")
        logger_server.info(f"[belt2] result: {result}")
        if result["detect"]:
            task_queue.put({
                "belt": "belt2",
                "class": result["class"]
            })
        return result
    else:
        last_belt2_time = belt2_time
        return {"status": "waiting", 
                "detect": 0, 
                "timestamp": None, 
                "class": ""}

# API：获取 K8s 节点信息
@app.get("/api/k8s_nodes")
async def get_k8s_nodes():
    return {"nodes": k8s_info['nodes'], "last_updated": k8s_info['last_updated']}

# API：获取 K8s Pod 信息
@app.get("/api/k8s_pods")
async def get_k8s_pods():
    return {"pods": k8s_info['pods'], "last_updated": k8s_info['last_updated']}

# WebSocket 端点：K8s 节点信息
@app.websocket("/ws/nodes")
async def websocket_nodes(websocket: WebSocket):
    await manager_nodes.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception as e:
        print(f"[nodes] WebSocket error: {e}")
    finally:
        manager_nodes.disconnect(websocket)

# WebSocket 端点：K8s Pod 信息
@app.websocket("/ws/pods")
async def websocket_pods(websocket: WebSocket):
    await manager_pods.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception as e:
        print(f"[pods] WebSocket error: {e}")
    finally:
        manager_pods.disconnect(websocket)

# WebSocket 端点：攻击检测信息
@app.websocket("/ws/attack")
async def websocket_attack(websocket: WebSocket):
    await manager_attack.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception as e:
        print(f"[attack] WebSocket error: {e}")
    finally:
        manager_attack.disconnect(websocket)

# FastAPI 启动事件
@app.on_event("startup")
async def startup_event():
    # 初始化 Redis 连接
    if init_redis():
        # 启动 Redis 监听线程
        loop = asyncio.get_event_loop()
        redis_thread = threading.Thread(target=redis_listener, args=(loop,), daemon=True)
        redis_thread.start()
        print("[Redis] 监听线程已启动")
    
    # 启动 SSH 任务监听器
    ssh_thread = threading.Thread(target=ssh_task_listener)
    ssh_thread.daemon = True
    ssh_thread.start()
    print("[SSH] 任务监听线程已启动")

# 如果直接运行此文件，则启动服务器
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)

# REDIS_HOST=10.96.252.224 uvicorn ws_server:app --host 0.0.0.0 --port 8000
