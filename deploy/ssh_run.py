"""用密码登录目标机，只执行只读巡检。密码从环境变量取，不写进命令行。"""
import os
import sys

import paramiko

host = os.environ["DEPLOY_HOST"]
user = os.environ.get("DEPLOY_USER", "root")
password = os.environ["DEPLOY_PASS"]

cmd = sys.argv[1] if len(sys.argv) > 1 else "uname -a"

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(host, username=user, password=password, timeout=20,
            allow_agent=False, look_for_keys=False)
stdin, stdout, stderr = cli.exec_command(cmd, timeout=120)
out = stdout.read().decode("utf-8", "replace")
err = stderr.read().decode("utf-8", "replace")
code = stdout.channel.recv_exit_status()
sys.stdout.write(out)
if err.strip():
    sys.stderr.write(err)
sys.exit(code)
