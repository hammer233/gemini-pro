#!/bin/bash
set -euo pipefail
DEBIAN_FRONTEND=noninteractive apt-get install -y libatomic1
if [ -x /usr/bin/google-chrome-stable ]; then
  echo "chrome already installed"
  google-chrome-stable --version
  exit 0
fi
cd /tmp
curl -fsSL -o google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
DEBIAN_FRONTEND=noninteractive apt-get install -y /tmp/google-chrome.deb
google-chrome-stable --version
rm -f /tmp/google-chrome.deb
echo "chrome installed"
