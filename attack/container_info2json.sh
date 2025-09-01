container_id="0e4dd8e2a56ab"  # 替换成你实际的容器 ID
output_file="/home/zs/${container_id}.json"

crictl inspect "$container_id" > "$output_file"
