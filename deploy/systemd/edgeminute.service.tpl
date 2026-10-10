[Unit]
Description=EdgeMinute (密纪) — Local Meeting Transcription + LLM Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=__USER__
WorkingDirectory=__EDGEMINUTE_HOME__
Environment=EDGEMINUTE_HOME=__EDGEMINUTE_HOME__
Environment=EDGEMINUTE_VENV=__EDGEMINUTE_VENV__
Environment=EDGEMINUTE_LLM=__LLM_ENABLED__
Environment=EDGEMINUTE_LLM_BIN=__LLAMACPP_DIR__/build/bin/llama-server
Environment=EDGEMINUTE_LLM_MODEL=__MODEL_PATH__
Environment=EDGEMINUTE_LLM_CTX=__CONTEXT_SIZE__
Environment=EDGEMINUTE_LLM_PORT=__LLM_PORT__
# 录音相关配置（RECORD_ENABLED / RECORD_DEVICE / RECORD_MAX_BYTES）走 EnvironmentFile
# 的 .env，不在单元里硬编码——见 .env.example 的「Microphone recording」一节。
EnvironmentFile=-__ENV_FILE__
# 麦克风录音：ffmpeg 需要打开 /dev/snd/*，那属于宿主机的 audio 组。
# 用 SupplementaryGroups 而不是 install.sh 里 usermod -aG audio —— 不依赖宿主机
# 组的持久变更，重装即生效；缺这一行录音会在打开设备时直接失败。
SupplementaryGroups=audio
ExecStart=__START_SCRIPT__
Restart=always
RestartSec=5
# LLM models can take a while to load; give the unit room before timing out.
TimeoutStartSec=0
# Crash logs go to journald: journalctl -u edgeminute -f
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
