#!/bin/bash
# 增强版节点安全检测脚本

LOG_FILE="./enhanced-node-detection.log"
ALERT_FILE="./enhanced-security-alerts.log"

echo "=== Enhanced Node Security Detection ===" | tee -a $LOG_FILE

# 检测攻击容器和进程
detect_attack_containers() {
    # 检测特权容器中的攻击脚本
    crictl ps -q 2>/dev/null | while read container_id; do
        if [ -n "$container_id" ]; then
            
            container_info=$(crictl inspect $container_id 2>/dev/null)
            pod_name=$(echo "$container_info" | jq -r 'if type == "array" then .[0].info.config.labels["io.kubernetes.pod.name"] else .info.config.labels["io.kubernetes.pod.name"] end // "unknown"')
            namespace=$(echo "$container_info" | jq -r 'if type == "array" then .[0].info.config.labels["io.kubernetes.pod.namespace"] else .info.config.labels["io.kubernetes.pod.namespace"] end // "default"')

            if echo "$container_info" | grep -q '"privileged": true'; then
                # 改进pod信息提取
                                
                if [ "$namespace" != "kube-system" ]; then
                    
                    # echo "[ALERT] Privileged container: $namespace/$pod_name ($container_id)" | tee -a $ALERT_FILE
                    
                    # 检查容器内的挂载操作
                    crictl exec $container_id mount 2>/dev/null | grep -E "(hostproc|/mnt.*proc)" | while read mount_info; do
                        # echo "[CRITICAL] Container escape detected in $namespace/$pod_name: $mount_info" | tee -a $ALERT_FILE
                        # echo "{\"pod_name\":\"$pod_name\",\"namespace\":\"$namespace\", \"alert\": \"Container escape detected:\" \"$mount_info\"}"
                        jq -n --arg pod_name "$pod_name" --arg namespace "$namespace"  --arg alert "Container escape detected: $mount_info"  '{pod_name: $pod_name, namespace: $namespace, alert: $alert}'
                    done
                    # 检查网络扫描活动（排除grep进程）
                    crictl exec $container_id ps aux 2>/dev/null | grep -E "(nmap|nc.*-z)" | grep -v grep | while read scan_proc; do
                        # echo "[CRITICAL] Network scanning in $namespace/$pod_name: $scan_proc" | tee -a $ALERT_FILE
                        jq -n --arg pod_name "$pod_name" --arg namespace "$namespace"  --arg alert "Illegal scan detected: $scan_proc"  '{pod_name: $pod_name, namespace: $namespace, alert: $alert}'
                    done

                fi
            fi
        fi
    done
}

# 检测反弹shell活动
detect_reverse_shell() {
    echo "[$(date)] Detecting reverse shell activities..." >> $LOG_FILE
    
    # 检查可疑网络连接
    ss -tuln 2>/dev/null | grep -E ":4444|:1337|:31337" | while read conn; do
        echo "[CRITICAL] Suspicious listening port detected: $conn" | tee -a $ALERT_FILE
    done
    
    # 检查外部连接
    ss -tupn 2>/dev/null | grep ESTAB | grep -v -E "127\.0\.0\.1|10\.|172\.|192\.168\." | while read ext_conn; do
        echo "[WARNING] External connection: $ext_conn" >> $LOG_FILE
    done
    
    # 检查容器内的反弹shell进程（排除grep进程）
    crictl ps -q 2>/dev/null | while read container_id; do
        if [ -n "$container_id" ]; then
            container_info=$(crictl inspect $container_id 2>/dev/null)
            pod_name=$(echo "$container_info" | jq -r '.[0].status.labels."io.kubernetes.pod.name" // "unknown"' 2>/dev/null)
            namespace=$(echo "$container_info" | jq -r '.[0].status.labels."io.kubernetes.pod.namespace" // "default"' 2>/dev/null)
            
            # 备用方法
            if [ "$pod_name" = "null" ] || [ "$pod_name" = "" ]; then
                pod_name=$(echo "$container_info" | grep -o '"io.kubernetes.pod.name":"[^"]*"' | cut -d'"' -f4)
                [ -z "$pod_name" ] && pod_name="unknown"
            fi
            
            crictl exec $container_id ps aux 2>/dev/null | grep -E "(nc.*-e|python.*socket|telnet.*[0-9])" | grep -v grep | while read shell_proc; do
                echo "[CRITICAL] Reverse shell attempt in $namespace/$pod_name: $shell_proc" | tee -a $ALERT_FILE
            done
        fi
    done
}

# 实时监控攻击日志
monitor_attack_logs() {
    echo "[$(date)] Starting attack log monitoring..." >> $LOG_FILE
    
    # 监控容器日志中的攻击活动
    crictl ps -q 2>/dev/null | while read container_id; do
        if [ -n "$container_id" ]; then
            container_info=$(crictl inspect $container_id 2>/dev/null)
            pod_name=$(echo "$container_info" | jq -r '.[0].status.labels."io.kubernetes.pod.name" // "unknown"' 2>/dev/null)
            
            # 备用方法
            if [ "$pod_name" = "null" ] || [ "$pod_name" = "" ]; then
                pod_name=$(echo "$container_info" | grep -o '"io.kubernetes.pod.name":"[^"]*"' | cut -d'"' -f4)
                [ -z "$pod_name" ] && pod_name="unknown"
            fi
            
            if [[ "$pod_name" == *"attack"* ]]; then
                echo "[INFO] Monitoring attack container logs: $pod_name" >> $LOG_FILE
                
                # 获取最近的日志并检查攻击活动
                crictl logs --tail=50 $container_id 2>/dev/null | grep -E "(CRITICAL|SUCCESS|CONTAINER ESCAPE)" | while read attack_log; do
                    echo "[ATTACK LOG] $pod_name: $attack_log" | tee -a $ALERT_FILE
                done
            fi
        fi
    done
}

# 启动检测
detect_attack_containers
# detect_reverse_shell
monitor_attack_logs
