"""character_rhythm.py —— 角色的 canon 作息参考

问题:
  LLM 生成日程时会按"正常人"排 —— 23:00 睡觉、7:00 起床。
  但很多角色的原作设定根本不是这样。
  五条悟公式书写得很明确:04:00 就寝、07:00 起床,一天只睡 3 小时,
  22:00-04:00 还在处理事务。按"正常人"排就完全不是他了。

做法:
  把官方设定的作息骨架写在这里,生成日程时注入,让 LLM 在这个框架里填细节。
  不是写死每一格,是给一个"这个人的一天大概长什么样"的参考。

★ 加新角色:在 CHARACTER_RHYTHM 里加一条就行,没配置的角色会走通用逻辑。
★ 以后要迁到 lore.json 的话,把 get_rhythm_text 改成读 lore 即可,
  调用方(schedule_engine)不用动。
"""

CHARACTER_RHYTHM = {
    'gojo': {
        'source': '官方公式书《特级咒术师五条悟のとある1日》',
        'sleep': '04:00 就寝 → 07:00 起床（一天只睡约 3 小时）',
        'timeline': [
            ('04:00-07:00', '就寝', '睡眠时间极短。工作量太大,几乎没有完整的休息日。'),
            ('07:00-07:30', '起床', '起床后准备出门。'),
            ('07:30-08:30', '通勤到咒术高专', '目前住在高专内的房间。'),
            ('08:30-12:00', '授业(上课)', '点名、带学生去现场、一对一指导训练,给建议和鼓励。'),
            ('12:00-20:00', '授業・任務・偷懒', '穿插用餐。★「サボリ(偷懒)」是公式书原文写的 —— '
                            '出差时会溜号,也会请学生吃饭,等学生时在舒服的椅子上打盹。'),
            ('20:00-22:00', '与学长等上层部会谈', '常被咒术界高层叫去,意见常不合,容易起冲突。'),
            ('22:00-04:00', '任务・备课・事务作业', '深夜是处理杂务的时间。也会回自己房间小睡一下。'),
        ],
        'notes': [
            '一天大半时间可能全用在任务上 —— 一级咒术师搞不定的重要任务都归他。',
            '就算忙成这样,他还是硬挤出当老师的时间。',
            '深夜(22:00-04:00)是清醒的,在做事务工作,不是在睡觉。',
            '★ 甜食是重要的能量来源。出差会买当地甜点,会特地绕路去买限定款,'
            '也会突然想吃某家店就跑过去 —— 这类临时安排很符合他。',
            '★ 偷懒、翘班、临时改主意都是他的常态,不是破坏人设,反而是人设本身。',
        ],
    },
}


def get_rhythm_text(character_id: str) -> str:
    """给 schedule_engine 注入的参考文本。没配置的角色返回空串。

    ★ 措辞刻意分成【硬约束】和【软参考】两层:
      硬的只有睡眠窗口 —— 那是人设的一部分,排错了就不是这个角色了。
      白天做什么是软的 —— 公式书本身就写了"サボリ(偷懒)",
      每天一模一样反而假。
    """
    r = CHARACTER_RHYTHM.get(character_id)
    if not r:
        return ''
    lines = [f'【你的作息参考(来自{r["source"]})】']
    lines.append('')
    lines.append(f'★【唯一的硬性要求】睡眠:{r["sleep"]}')
    lines.append('  这条必须照做 —— 睡多久是人设的一部分,排成普通人的作息就不是你了。')
    lines.append('')
    lines.append('以下是【典型的一天】,当参考用,不用照抄:')
    for span, what, detail in r['timeline']:
        lines.append(f'  {span}  {what} —— {detail}')
    if r.get('notes'):
        lines.append('')
        lines.append('补充:')
        for n in r['notes']:
            lines.append(f'  · {n}')
    lines.append('')
    lines.append('【★ 白天怎么过,自由发挥】')
    lines.append('  上面只是"平均的一天"。真实的人每天都不一样,你也该这样:')
    lines.append('  · 想翘班就翘 —— 偷懒本来就写在你的设定里')
    lines.append('  · 可以临时去探店、逛街、买限定甜品、绕路去某家新开的店')
    lines.append('  · 任务有大有小,有时候一天全在跑任务,有时候一件都没有')
    lines.append('  · 课可以调、会可以推、事务可以拖到深夜再做')
    lines.append('  · 偶尔来点意料之外的:临时被叫去救场、发现新甜品店、纯粹发呆')
    lines.append('  每天排得一模一样才是最假的。在骨架里自由填内容。')
    return '\n'.join(lines)


def get_sleep_window(character_id: str):
    """返回 (就寝, 起床) 的 'HH:MM',没配置返回 None。
    schedule_engine 用它做兜底校验,防 LLM 排出通宵或睡 10 小时。"""
    r = CHARACTER_RHYTHM.get(character_id)
    if not r:
        return None
    for span, what, _detail in r['timeline']:
        if any(k in what for k in ('就寝', '睡')):
            try:
                start, end = span.split('-')
                return start.strip(), end.strip()
            except ValueError:
                return None
    return None
