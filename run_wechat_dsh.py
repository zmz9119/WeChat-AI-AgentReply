"""run_wechat_dsh.py - 用指定配置启动 bot。

bot 本体（wechat_ai_bot_dev.py）与 bridge（hermes_bridge_core.py）零改动。
原因：这两个模块都在**调用时**才读模块级全局——

    wechat_ai_bot_dev.load_config()            ->  读 CONFIG_FILE
    hermes_bridge_core.HermesBridge.__init__   ->  读 CONVO_MAP_FILE

所以这里 import 之后覆写这两个全局，再调用同一个 main_loop()，走的仍是
原逻辑。hermes_api 字段被原样透传给 HermesClient，本文件不需要关心它指向
Hermes 还是 DSH——换个配置文件就换了后端。

用法:
    python run_wechat_dsh.py                          用默认的 wechat_config_dsh.json
    python run_wechat_dsh.py wechat_config_dev.json   临时切到另一份配置
    python run_wechat_dsh.py --check                  只校验，不进主循环

会话映射文件按配置名自动派生（wechat_config_X.json ->
wechat_convo_map_X.json），避免两套配置共用一张映射表互相串会话。
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = "wechat_config_dsh.json"


def resolve(config_name):
    """把配置名解析成 (config 路径, 会话映射路径)。"""
    candidate = (
        config_name
        if os.path.isabs(config_name)
        else os.path.join(BASE_DIR, config_name)
    )
    if not os.path.exists(candidate):
        raise SystemExit(f"config not found: {candidate}")

    stem = os.path.splitext(os.path.basename(candidate))[0]
    prefix = "wechat_config"
    suffix = stem[len(prefix):].lstrip("_") if stem.startswith(prefix) else stem
    return candidate, os.path.join(
        os.path.dirname(candidate), f"wechat_convo_map_{suffix}.json"
    )


def apply_config(config_file, convo_map_file):
    """把 bot 与 bridge 指向指定配置。

    返回 (bot 模块, bridge 模块, 加载到的配置)。
    """
    sys.path.insert(0, BASE_DIR)

    # 先 import bridge，这样 main_loop() 里的
    # `from hermes_bridge_core import HermesBridge` 会命中 sys.modules 缓存，
    # 拿到已覆写 CONVO_MAP_FILE 的那个模块。
    import hermes_bridge_core
    import wechat_ai_bot_dev as bot

    bot.CONFIG_FILE = config_file
    hermes_bridge_core.CONVO_MAP_FILE = convo_map_file
    return bot, hermes_bridge_core, bot.load_config()


def check(argv):
    """打印实际生效的配置，便于确认没有静默落到别的后端。"""
    config_file, convo_map = resolve(argv[0] if argv else DEFAULT_CONFIG)
    bot, bridge, cfg = apply_config(config_file, convo_map)
    api = cfg.get("hermes_api", "?")
    print(f"config file          : {bot.CONFIG_FILE}")
    print(f"convo map            : {bridge.CONVO_MAP_FILE}")
    print(f"upstream (hermes_api): {api}")
    print(f"bridge_port          : {cfg.get('bridge_port', 8900)}")
    print(f"bridge_chat_timeout  : {cfg.get('bridge_chat_timeout', 180)}")
    print(f"hermes_cli_path      : {cfg.get('hermes_cli_path', '') or '(disabled)'}")
    print(f"whitelist            : {cfg.get('whitelist', [])}")

    client = bridge.HermesClient(api, cfg.get("hermes_api_key", ""))
    health = client.health()
    print(f"upstream health      : {health}")
    # Hermes 返 {"status":"ok"}，DSH 插件返 {"ok":true}，两种都算活着。
    alive = health.get("ok") is True or health.get("status") == "ok"
    if alive:
        print("OK: upstream agent entry reachable")
        return 0
    print("WARN: upstream agent entry unreachable - is it running?")
    return 0


def main():
    argv = list(sys.argv[1:])
    if "--check" in argv:
        argv.remove("--check")
        raise SystemExit(check(argv))

    config_file, convo_map = resolve(argv[0] if argv else DEFAULT_CONFIG)
    print(f"[launch] config : {config_file}")
    print(f"[launch] convo  : {convo_map}", flush=True)
    bot, _, _ = apply_config(config_file, convo_map)
    bot.main_loop()


if __name__ == "__main__":
    main()
