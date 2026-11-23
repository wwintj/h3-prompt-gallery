#!/bin/bash
# 双击即可卸载（会先确认；Google Drive 里的作品和提示词不受影响）
bash "$(dirname "$0")/uninstall.sh"
echo; read -r -p "按回车关闭窗口" _
