#!/usr/bin/env python3
import subprocess
import time
import redis
import os
import json

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
            ['sudo', 'bash', 'enhanced-node-detect.sh'],
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
        
        # 简单过滤，只保留包含关键词的行
        
        # print(json.load(result.stdout))
        # return json.load(result.stdout)
        return result.stdout
        
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
        print(f"alerts: {alerts}")
        if alerts:
            print(f"Found alerts")
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