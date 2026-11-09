"""Fixed semantic evaluation, deliberately separate from deterministic tests.

Default commands only generate fixtures/templates or score supplied evidence.
This program never connects to a model or runs business tools. Collect real
model receipts in an isolated Meido role, then import them for scoring.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
import statistics

SKILLS = {
    'meeting-notes': ('从已经提供的会议记录整理决定、负责人、截止时间和待确认项。一般闲聊、写邀请或没有会议原文时不使用。',
        '先核对输入是否含会议原文；没有则询问。逐项整理决定、负责人和截止时间；原文未写的信息标待确认。输出行动项表与未决问题，最后逐项对照原文，不能虚构承诺。'),
    'evidence-brief': ('根据提供的多条材料写可追溯的工作简报，区分已验证事实与待确认信息。普通闲聊、只改一句话和无材料的创意写作不使用。',
        '先读取主人提供的真实材料；区分已验证事实、推断和待确认项。简报列成果、证据来源和下一步；引用可追溯到输入，不能把未执行动作写成完成。'),
    'proofread': ('主人提供完整文本并要求严格校对错别字、标点和歧义时使用。闲聊、翻译、解释术语与自由创作不使用。',
        '保留原意，逐句检查错别字、标点和歧义。输出修改后全文和必要改动说明；不擅自增加事实；可能改变原意的地方列为待确认。'),
}


def cases():
    positives = [
        ('meeting-notes', '下面是会议记录：小林周五交稿，阿秋负责核验，发布时间尚未定。帮我做一份可执行的会后记录。'),
        ('meeting-notes', '从这段讨论提炼谁要做什么：甲说本周跑测试，乙确认下周一整理反馈。'),
        ('meeting-notes', '请把会议原文变成决定与跟进清单：决定采用方案乙；负责人还没确定；下月复查。'),
        ('meeting-notes', '整理会后行动：我们商定今天修复登录问题，负责人安娜；其他问题待下次会议。不要猜没写的时间。'),
        ('evidence-brief', '根据材料做给主管看的周报：来源A记录接口测试通过；来源B说上线还在等待确认。请注明依据。'),
        ('evidence-brief', '把两份进展合成一份可核查简报：日志写修复已验收；讨论串只提出下周部署，尚未执行。'),
        ('evidence-brief', '这些素材需要一份成果说明：测试记录显示48项通过，发布单仍是草稿。帮我分清已完成和下一步。'),
        ('evidence-brief', '据这两条事实写工作汇报并标来源：工单12已验证修复；工单13尚未复现，不要夸大进展。'),
        ('proofread', '严格校对这段通知并保留原意：请在周五之前提交报高，逾期将延期处理。。'),
        ('proofread', '帮我逐句检查错字与标点，列必要改动：本次活动以经结束，感谢各位的参予。'),
        ('proofread', '这段说明要正式发布，请检查文字错误和歧义：系统维护从下午三点开始至四点结束后恢复。'),
        ('proofread', '不改内容，只做文字校对并解释改动：我们在这里诚至邀请你参加明天的座谈会，，请准时到厂。'),
    ]
    negatives = ['你好，今天心情怎么样？', '谢谢你陪我。', '给我讲一个小笑话。', '二加三是多少？',
        '解释一下什么是晴天。', '把 hello 翻译成中文。', '我今天有点累。', '晚安，明天见。',
        '请写一句生日祝福。', '“会议纪要”是什么意思？', '你最喜欢什么颜色？', '给我的猫取个名字。']
    adjacent = [
        ('帮我发一个会议邀请，时间是周四三点。', [], '创作邀请，不是整理会议记录。'),
        ('我有一场会议，先陪我聊聊紧张的心情。', [], '情绪聊天不应因为会议一词触发。'),
        ('周报这个词的英文是什么？', [], '翻译词语不需要简报方法。'),
        ('我想写周报但现在没有材料，你先问我需要提供什么。', [], '可以先澄清，不应编造已核验材料。'),
        ('把这句话写得更诗意：风吹过窗台。', [], '创意改写不属于严格校对。'),
        ('下面是会后总结，请核对错字但不要再提取行动项：大家以经离开。', ['proofread'], '相邻技能应服从本轮校对要求。'),
    ]
    return ([{'id': f'p{i+1:02}', 'kind': 'positive', 'prompt': prompt, 'expectedSkillIds': [skill],
              'outcomeCheck': SKILLS[skill][1]} for i, (skill, prompt) in enumerate(positives)]
        + [{'id': f'n{i+1:02}', 'kind': 'negative', 'prompt': prompt, 'expectedSkillIds': [],
            'outcomeCheck': '直接完成当前请求，没有无关方法读取。'} for i, prompt in enumerate(negatives)]
        + [{'id': f'a{i+1:02}', 'kind': 'adjacent', 'prompt': prompt, 'expectedSkillIds': skills,
            'outcomeCheck': check} for i, (prompt, skills, check) in enumerate(adjacent)])


def template():
    return [{**case, 'repeat': repeat, 'model': '', 'readSkillIds': None, 'completed': None,
        'requests': None, 'latencyMs': None, 'inputTokens': None, 'outputTokens': None,
        'evidence': '', 'outcomeNotes': ''} for case in cases() for repeat in range(1, 4)]


def score(records):
    expected = {case['id']: case for case in cases()}
    groups = defaultdict(list)
    seen = set()
    for item in records:
        key = item.get('model'), item.get('id'), item.get('repeat')
        if not isinstance(key[0], str) or not key[0].strip() or key[1] not in expected or key[2] not in {1, 2, 3} or key in seen:
            raise ValueError('每项证据必须有模型、固定 case ID 和唯一的 1..3 重复编号')
        if (not isinstance(item.get('readSkillIds'), list) or any(not isinstance(value, str) for value in item['readSkillIds'])
                or not isinstance(item.get('completed'), bool) or not item.get('evidence')):
            raise ValueError('缺少真实读取列表、成果判定或可追溯证据，不能计为模型评估')
        for field in ('requests', 'latencyMs', 'inputTokens', 'outputTokens'):
            if item.get(field) is not None and (isinstance(item[field], bool) or not isinstance(item[field], (int, float)) or item[field] < 0):
                raise ValueError(f'{field} 必须是非负数或 null')
        seen.add(key)
        groups[key[0]].append(item)
    reports = {}
    for model, items in groups.items():
        by_kind = defaultdict(list)
        for item in items:
            by_kind[expected[item['id']]['kind']].append(item)
        positive = by_kind['positive']
        negative = by_kind['negative']
        hit = sum(set(expected[item['id']]['expectedSkillIds']) <= set(item['readSkillIds']) for item in positive)
        false = sum(bool(item['readSkillIds']) for item in negative)
        complete = len(items) == len(expected) * 3
        hit_rate, false_rate = hit / len(positive) if positive else None, false / len(negative) if negative else None
        reports[model] = {'samples': len(items), 'completeMatrix': complete,
            'coverage': dict(Counter(expected[item['id']]['kind'] for item in items)),
            'positiveTriggerRate': hit_rate, 'negativeFalseTriggerRate': false_rate,
            'completedRate': sum(item['completed'] for item in items) / len(items),
            'adjacentExactChoiceRate': (sum(set(item['readSkillIds']) == set(expected[item['id']]['expectedSkillIds'])
                for item in by_kind['adjacent']) / len(by_kind['adjacent']) if by_kind['adjacent'] else None),
            'initialSuggestedThresholdMet': complete and hit_rate >= .9 and false_rate <= .05,
            'costs': {field: statistics.mean(values) if (values := [item[field] for item in items if item.get(field) is not None]) else None
                     for field in ('requests', 'latencyMs', 'inputTokens', 'outputTokens')}}
    return {'evaluationType': 'real-model-evidence', 'models': reports,
        'note': '程序正确性测试不等于语义质量。阈值仅为初始建议，不是模型保证；未完成矩阵不能通过。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--write-template', type=Path)
    action.add_argument('--write-fixtures', type=Path)
    action.add_argument('--results', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.write_template:
        args.write_template.parent.mkdir(parents=True, exist_ok=True)
        args.write_template.write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in template()), encoding='utf-8')
    elif args.write_fixtures:
        for name, (description, body) in SKILLS.items():
            path = args.write_fixtures / name / 'SKILL.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'---\nname: {name}\ndescription: {description}\n---\n{body}\n', encoding='utf-8')
    else:
        result = score([json.loads(line) for line in args.results.read_text(encoding='utf-8').splitlines() if line.strip()])
        raw = json.dumps(result, ensure_ascii=False, indent=2)
        if args.report:
            args.report.write_text(raw + '\n', encoding='utf-8')
        else:
            print(raw)


if __name__ == '__main__':
    main()
