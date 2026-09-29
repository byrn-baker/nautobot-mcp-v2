# Disposable Nautobot Nautobot 3.1.8 + public plugin versions, for write-tool tests.
FROM networktocode/nautobot:3.1.8-py3.12

RUN pip install --no-cache-dir \
    nautobot-bgp-models==3.1.1 \
    nautobot-golden-config==3.0.6 \
    nautobot-plugin-nornir==3.2.1

RUN printf '%s\n' \
  'PLUGINS = ["nautobot_plugin_nornir", "nautobot_golden_config", "nautobot_bgp_models"]' \
  'PLUGINS_CONFIG = {' \
  '    "nautobot_plugin_nornir": {"nornir_settings": {' \
  '        "credentials": "nautobot_plugin_nornir.plugins.credentials.env_vars.CredentialsEnvVars",' \
  '        "runner": {"plugin": "threaded", "options": {"num_workers": 4}}}},' \
  '    "nautobot_golden_config": {},' \
  '}' >> /opt/nautobot/nautobot_config.py
