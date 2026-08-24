# test_relationship.py
from relationship_engine import process_turn
from relationship_reader import build_state_summary

result = process_turn(
    user_id='test_user_1',
    character_id='gojo',
    user_message='你今天真好看，晚上想不想跟我聊到很晚',
    character_reply='……你谁？',
)
print('=== process_turn 返回 ===')
print(result)

print('=== 当前状态摘要 ===')
print(build_state_summary('test_user_1', 'gojo'))