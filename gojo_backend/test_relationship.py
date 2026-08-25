# 直接同步调用，不走 threading，任何东西都会打印出来
from relationship_engine import process_turn
from relationship_reader import build_state_summary

print("=== 开始 process_turn ===")
try:
    r = process_turn(
        user_id='probe_manual_1',
        character_id='gojo',
        user_message='你今天真好看，晚上想不想跟我聊到很晚',
        character_reply='……你谁？',
    )
    print("=== process_turn 返回 ===")
    import json
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
except Exception as e:
    import traceback
    print("=== process_turn 出错 ===")
    traceback.print_exc()

print("\n=== build_state_summary ===")
try:
    print(build_state_summary('probe_manual_1', 'gojo'))
except Exception as e:
    import traceback
    traceback.print_exc()