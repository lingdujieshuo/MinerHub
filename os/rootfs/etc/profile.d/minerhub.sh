# 登录时显示矿机状态
if [ -n "$PS1" ] && [ -z "$MH_SHOWN" ] && command -v mh >/dev/null; then
  export MH_SHOWN=1
  echo; mh status 2>/dev/null | head -n 20; echo
  echo "命令: mh status | mh log | mh agent | mh config | mh update | mh update-srb"
  echo
fi
