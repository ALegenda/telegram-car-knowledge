"""Short-lived SSH runner. Only the ephemeral public key and ciphertext enter Git.

The private key stays in this process. A matching workflow run/commit must be
verified by the caller before supplying an encrypted payload. No remote shell
or arbitrary command is accepted from the payload.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def main():
    import paramiko
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    revision = os.environ["GITHUB_SHA"]
    branch = "bot-session/" + run_id
    mode = Path("deploy/remote-session.request").read_text().splitlines()[0].strip()
    if mode not in {"inspect", "prepare", "deploy"}:
        raise ValueError("Unsupported session mode")

    def api(method, path, body=None, missing=False):
        request = urllib.request.Request(
            "https://api.github.com/repos/" + repository + "/" + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                     "Accept": "application/vnd.github+json", "User-Agent": "bot-remote-session"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as exc:
            if missing and exc.code == 404:
                return None
            raise RuntimeError(f"GitHub session API status {exc.code}") from None

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    created = False
    client = None
    try:
        api("POST", "git/refs", {"ref": "refs/heads/" + branch, "sha": revision})
        created = True
        api("PUT", "contents/deploy/session-public-key.pem", {
            "branch": branch, "message": "Publish ephemeral bot session public key",
            "content": base64.b64encode(public).decode(),
        })
        print(f"Ephemeral receiver ready: {branch}; operation={mode}", flush=True)
        payload_file = None
        for _ in range(150):
            payload_file = api(
                "GET", "contents/deploy/session-payload.json?ref=" + urllib.parse.quote(branch),
                missing=True,
            )
            if payload_file:
                break
            time.sleep(3)
        if not payload_file:
            raise RuntimeError("No encrypted payload received before session expired")
        envelope = json.loads(base64.b64decode(payload_file["content"]))
        aes_key = key.decrypt(base64.b64decode(envelope["key"]), padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None,
        ))
        payload = json.loads(AESGCM(aes_key).decrypt(
            base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["data"]),
            run_id.encode(),
        ))
        if payload["run_id"] != run_id or payload["revision"] != revision or payload["mode"] != mode:
            raise ValueError("Session binding does not match")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Invalid release")
        expected_host_key = payload.get("host_key")
        if mode != "inspect" and not expected_host_key:
            raise ValueError("Server host key from inspection is required")

        class PinnedHostKey(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                actual = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
                if expected_host_key and actual != expected_host_key.removeprefix("SHA256:"):
                    raise paramiko.SSHException("Server host key mismatch")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(PinnedHostKey())
        client.connect(
            payload["host"], port=payload.get("port", 22), username=payload["user"],
            password=payload["password"], timeout=15, banner_timeout=15, auth_timeout=15,
            look_for_keys=False, allow_agent=False,
        )
        fingerprint = base64.b64encode(hashlib.sha256(
            client.get_transport().get_remote_server_key().asbytes()
        ).digest()).decode().rstrip("=")
        print("SSH authenticated; host key SHA256:" + fingerprint, flush=True)

        def command(text):
            _, stdout, stderr = client.exec_command(text)
            output = stdout.read().decode(errors="replace")
            errors = stderr.read().decode(errors="replace")
            status = stdout.channel.recv_exit_status()
            if output:
                print(output, end="", flush=True)
            if errors:
                print(errors, end="", flush=True)
            if status:
                raise RuntimeError(f"Remote operation failed with exit status {status}")

        if mode == "inspect":
            command("""python3 - <<'PY'
import json, os, pathlib, shutil, subprocess, sys
print(json.dumps({
 'python':sys.version.split()[0], 'cpus':os.cpu_count(),
 'disk_free_gib':round(shutil.disk_usage('/opt').free/1024**3,1),
 'systemd':bool(shutil.which('systemctl')),
 'bot_installed':pathlib.Path('/opt/tg-knowledge-bot/current').exists(),
 'memory':next((line.strip() for line in pathlib.Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:')), 'unknown')
}, indent=2))
print(subprocess.check_output(['systemctl','list-units','--type=service','--state=running','--no-pager','--plain'],text=True))
PY""")
        else:
            config = payload.get("config", "")
            from dotenv import dotenv_values
            import io

            values = dotenv_values(stream=io.StringIO(config))
            if mode == "deploy" and not values.get("TELEGRAM_BOT_TOKEN"):
                with client.open_sftp() as sftp:
                    with sftp.open("/etc/tg-knowledge-bot/telegram.env", "r") as source:
                        token_config = source.read().decode()
                config += "\n" + token_config
                values = dotenv_values(stream=io.StringIO(config))
            if mode == "deploy" and (not values.get("OPENAI_API_KEY") or not values.get("TELEGRAM_BOT_TOKEN")):
                raise ValueError("Missing bot credentials; server was not modified")
            command("umask 077; mkdir -p /opt/tg-knowledge-bot/uploads")
            with tempfile.TemporaryDirectory() as temporary:
                archive = str(Path(temporary) / "release.tar.gz")
                subprocess.run(["git", "archive", "--format=tar.gz", "--output", archive, revision], check=True)
                remote = "/opt/tg-knowledge-bot/uploads/" + revision
                with client.open_sftp() as sftp:
                    sftp.put(archive, remote + ".tar.gz")
                    if mode == "deploy":
                        with sftp.open(remote + ".env", "w") as output:
                            output.write(config)
                        sftp.chmod(remote + ".env", 0o600)
                    sftp.put("scripts/install-bot.sh", remote + ".install.sh")
                command("bash " + remote + ".install.sh " + revision + (" prepare" if mode == "prepare" else ""))
                command("rm -f " + remote + ".install.sh")
    finally:
        if client:
            client.close()
        if created:
            api("DELETE", "git/refs/heads/" + branch)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never print exception text: libraries may include credentials/URLs.
        print("Remote session failed: " + type(error).__name__, flush=True)
        raise SystemExit(1) from None
