"""Send a sealed payload to a verified running bot-remote-session workflow.

Requires gh authentication and cryptography. The password is read with getpass;
it never appears in argv, Git content, process output or a persistent local file.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import subprocess
from pathlib import Path


def main():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--user", default="root")
    parser.add_argument("--mode", choices=["inspect", "deploy"], required=True)
    parser.add_argument("--env", type=Path)
    args = parser.parse_args()

    def gh(*parts, data=None):
        result = subprocess.run(["gh", *parts], input=data, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError("GitHub request failed")
        return json.loads(result.stdout)

    run = gh("api", f"repos/{args.repo}/actions/runs/{args.run_id}")
    expected = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if run["head_sha"] != expected or run["head_branch"] != "codex/telegram-rag-bot" or run["status"] != "in_progress":
        raise ValueError("Workflow must be running at the locally reviewed commit")
    if run["path"] != ".github/workflows/bot-remote-session.yml":
        raise ValueError("Unexpected receiver workflow")
    branch = "bot-session/" + args.run_id
    public_file = gh("api", f"repos/{args.repo}/contents/deploy/session-public-key.pem?ref={branch}")
    public = serialization.load_pem_public_key(base64.b64decode(public_file["content"]))
    if public.key_size != 4096:
        raise ValueError("Unexpected receiver key size")
    payload = {
        "host": args.host, "port": args.port, "user": args.user,
        "mode": args.mode, "run_id": args.run_id, "revision": expected,
    }
    if args.mode == "deploy":
        if not args.env:
            raise ValueError("An ignored env file is required")
        payload["config"] = args.env.read_text(encoding="utf-8")
    payload["password"] = getpass.getpass("SSH password (hidden): ")
    key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    data = AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), args.run_id.encode())
    envelope = {
        "key": base64.b64encode(public.encrypt(key, padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None,
        ))).decode(),
        "nonce": base64.b64encode(nonce).decode(), "data": base64.b64encode(data).decode(),
    }
    gh("api", "--method", "PUT", f"repos/{args.repo}/contents/deploy/session-payload.json", "--input", "-", data=json.dumps({
        "branch": branch, "message": "Deliver sealed one-time SSH session payload",
        "content": base64.b64encode(json.dumps(envelope).encode()).decode(),
    }))
    print("Encrypted session payload delivered; no plaintext credentials published.")


if __name__ == "__main__":
    main()
