#!/usr/bin/env python3
import subprocess
import time
import redis
import os

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

def run_detection():
    """运行检测脚本并获取输出"""
    try:
        # 运行脚本
        result = subprocess.run(
            ['bash', './enhanced-node-detect.sh'],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        # 打印所有输出
        print(f"=== Return Code: {result.returncode} ===")
        print(f"=== STDOUT ===")
        print(result.stdout)
        print(f"=== STDERR ===") 
        print(result.stderr)
        
        # 合并输出
        all_output = result.stdout + result.stderr
        
        # 简单过滤，只保留包含关键词的行
        alert_lines = []
        for line in all_output.split('\n'):
            if any(keyword in line for keyword in ['[ALERT]', '[CRITICAL]', '[WARNING]']):
                # 如果[ALERT]开头，那么解析记录当前的特权容器是哪个
                if line.startswith('[ALERT] Privileged container:'):
                    print(f"Privileged container detected: {line}")
                    alert_lines.append(line)
                # 如果是[CRITICAL] Container escape detected开头，并且包含/mnt/hostproc，那么记录这次的疑似逃逸的攻击
                if line.startswith('[CRITICAL] Container escape detecteD'):
                    if '/mnt/hostproc' in line:
                        print(f"Container escape detected: {line}")
                        alert_lines.append(line)
                if line.startswith('[CRITICAL] Network scanning'):
                    if 'nc -z -w1' in line:
                        print(f"Net scanning detected: {line}")
                        alert_lines.append(line)
        
        print(f"=== Filtered Alerts ===")
        for line in alert_lines:
            print(line)
            
        return alert_lines
        
    except subprocess.TimeoutExpired:
        print("Script timeout!")
        return []
    except Exception as e:
        print(f"Error: {e}")
        return []

def main():
    """主循环"""
    print("Starting simple monitor...")
    redis_client = setup_redis()
    
    while True:
        print(f"\n--- Running detection at {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
        alerts = run_detection()
        
        if alerts:
            print(f"Found {len(alerts)} alerts")
            # for alert in alerts:
            redis_client.publish('security_alerts', alerts)
        else:
            print("No alerts found")
        
        print("Waiting 10 seconds...")
        time.sleep(1)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user")