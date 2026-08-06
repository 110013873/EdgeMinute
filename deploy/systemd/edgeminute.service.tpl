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
EnvironmentFile=-__ENV_FILE__
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
