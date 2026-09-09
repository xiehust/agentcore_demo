"""Run on the NEW K3s node through SSM. Never prints private keys or kubeconfig."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(*args, input=None):
    return subprocess.check_output(args, input=input, text=True).strip()


def kubectl(*args, input=None):
    return run("/usr/local/bin/k3s", "kubectl", *args, input=input)


def apply(obj):
    kubectl("apply", "-f", "-", input=json.dumps(obj))


def main():
    region, secret_arn, private_ip = sys.argv[1:]
    os.umask(0o077)
    # AL2023 first-boot updates can hold the RPM lock while SSM is already online.
    subprocess.run(["cloud-init", "status", "--wait"], check=True, timeout=300)
    subprocess.run(["dnf", "install", "-y", "openssl"], check=True)
    run("curl", "-fsSL", "--retry", "3", "https://get.k3s.io", "-o", "/tmp/install-k3s.sh")
    env = dict(os.environ, INSTALL_K3S_VERSION="v1.36.4+k3s1")
    subprocess.run(["sh", "/tmp/install-k3s.sh", "server", "--disable", "traefik",
                    "--disable", "servicelb", "--disable", "metrics-server",
                    "--disable", "local-storage", "--tls-san", private_ip,
                    "--kube-apiserver-arg", "anonymous-auth=false"], env=env, check=True)
    for _ in range(90):
        try:
            kubectl("get", "nodes")
            break
        except subprocess.CalledProcessError:
            time.sleep(5)
    kubectl("wait", "--for=condition=Ready", "node", "--all", "--timeout=240s")
    apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "mtls-demo"}})
    apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": "mtls-proof", "namespace": "mtls-demo"}, "data": {
            "message": "AgentCore Runtime reached private K3s with an X.509 client certificate",
            "expected_user": "agentcore-mtls-reader"}})
    apply({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
           "metadata": {"name": "mtls-node-reader"}, "rules": [
               {"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list"]}]})
    apply({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
           "metadata": {"name": "mtls-node-reader"}, "subjects": [
               {"kind": "User", "name": "agentcore-mtls-reader", "apiGroup": "rbac.authorization.k8s.io"}],
           "roleRef": {"kind": "ClusterRole", "name": "mtls-node-reader", "apiGroup": "rbac.authorization.k8s.io"}})
    apply({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {
        "name": "mtls-reader", "namespace": "mtls-demo"}, "rules": [
            {"apiGroups": [""], "resources": ["configmaps", "pods"], "verbs": ["get", "list"]}]})
    apply({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {
        "name": "mtls-reader", "namespace": "mtls-demo"}, "subjects": [
            {"kind": "User", "name": "agentcore-mtls-reader", "apiGroup": "rbac.authorization.k8s.io"}],
        "roleRef": {"kind": "Role", "name": "mtls-reader", "apiGroup": "rbac.authorization.k8s.io"}})
    with tempfile.TemporaryDirectory(prefix="mtls-bootstrap-") as tmp:
        root = Path(tmp)
        key, csr = root / "client.key", root / "client.csr"
        run("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
            "-out", str(csr), "-subj", "/CN=agentcore-mtls-reader")
        csr_name = "agentcore-mtls-" + str(int(time.time()))
        apply({"apiVersion": "certificates.k8s.io/v1", "kind": "CertificateSigningRequest",
               "metadata": {"name": csr_name}, "spec": {
                   "request": base64.b64encode(csr.read_bytes()).decode(),
                   "signerName": "kubernetes.io/kube-apiserver-client",
                   "expirationSeconds": 86400, "usages": ["client auth"]}})
        kubectl("certificate", "approve", csr_name)
        cert = ""
        for _ in range(30):
            obj = json.loads(kubectl("get", "csr", csr_name, "-o", "json"))
            cert = obj.get("status", {}).get("certificate", "")
            if cert:
                break
            time.sleep(2)
        if not cert:
            raise RuntimeError("CSR not signed")
        bad_key, bad_cert = root / "untrusted.key", root / "untrusted.crt"
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(bad_key), "-out", str(bad_cert), "-subj", "/CN=agentcore-mtls-reader",
            "-addext", "extendedKeyUsage=clientAuth")
        admin_config = json.loads(kubectl("config", "view", "--raw", "-o", "json"))
        server_ca = base64.b64decode(admin_config["clusters"][0]["cluster"]["certificate-authority-data"]).decode()
        bundle = {"server": f"https://{private_ip}:6443", "server_ca": server_ca,
                  "client_cert": base64.b64decode(cert).decode(), "client_key": key.read_text(),
                  "untrusted_cert": bad_cert.read_text(), "untrusted_key": bad_key.read_text(),
                  "wrong_ca": bad_cert.read_text()}
        secret_file = root / "bundle.json"
        secret_file.write_text(json.dumps(bundle))
        run("aws", "secretsmanager", "put-secret-value", "--region", region,
            "--secret-id", secret_arn, "--secret-string", f"file://{secret_file}")
        print(json.dumps({"status": "ready", "server": bundle["server"],
                          "certificate_user": "agentcore-mtls-reader", "certificate_lifetime_seconds": 86400}))


if __name__ == "__main__":
    main()
