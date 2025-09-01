from kubernetes import client, config
from kubernetes.client.rest import ApiException

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

    # Step 3: 删除节点
    try:
        core_api.delete_node(name=node_name)
        print(f"[INFO] Deleted node '{node_name}' from cluster")
    except ApiException as e:
        print(f"[ERROR] Failed to delete node: {e}")

# 示例用法
if __name__ == "__main__":
    delete_malicious_pod_and_node(pod_name="attack-simulator", namespace="security-playground")
