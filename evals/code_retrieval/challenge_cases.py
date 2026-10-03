"""Predeclared three-arm Agent challenge. Labels are outside searchable roots."""
from pathlib import Path

JROOT = 'src/main/java/org/springframework/samples/petclinic/'


def evidence(root, path, first, last, *anchors):
    lines = (root / path).read_text(encoding='utf-8').splitlines()
    start = next(i for i, line in enumerate(lines, 1) if first in line)
    end = next(i for i, line in enumerate(lines, 1) if i >= start and last in line)
    points = [next(i for i, line in enumerate(lines, 1) if start <= i <= end and value in line) for value in anchors]
    return dict(path=path, start=start, end=end, anchors=points,
                anchor_text={str(i): lines[i-1] for i in points})


def cases(roots):
    p, j, t = (roots[name] for name in ('click', 'petclinic', 'agentdemo'))
    def P(path, *args): return evidence(p, 'src/click/' + path + '.py', *args)
    def J(path, *args): return evidence(j, JROOT + path + '.java', *args)
    def T(path, *args): return evidence(t, 'server/services/' + path + '.ts', *args)
    rows = []
    def locate(id, source, kind, query, targets):
        rows.append(dict(id=id, source=source, kind=kind, query=query, targets=targets))
    locate('P1','click','exact', '定位 UUIDParameterType.convert，给出它直接接受 UUID 对象、处理字符串空白并报告非法输入的源码证据。',
        [P('types','class UUIDParameterType','return "UUID"','value = value.strip()','return uuid.UUID(value)')])
    locate('P2','click','semantic', '命令帮助摘要在哪里区分句号结束一句话和 vs. 之类的缩写？请找出查看下一个单词首字母大小写的具体判断。',
        [P('utils','def _make_default_short_help','return textwrap.shorten','not words[i + 1][0].islower()')])
    locate('P3','click','cross_file', '没有手工提供简短帮助时，哪个命令方法会生成摘要，它调用哪个工具函数只采用长帮助的第一段？请同时提供调用入口与第一段截取的证据。',
        [P('core','def get_short_help_str','return text.strip()','text = _make_default_short_help'),
         P('utils','def _make_default_short_help','return textwrap.shorten','words = help.partition')])
    locate('P6','click','absent','这个源码范围内，OAuth 设备授权码轮询、刷新访问令牌的实现在哪里？',[])
    locate('J1','petclinic','exact','定位 Vet.getSpecialties，证明它返回按专业名称排序的列表，而不是直接返回内部集合。',
        [J('vet/Vet','public List<Specialty> getSpecialties()','.collect(Collectors.toList());','.sorted(Comparator.comparing')])
    locate('J2','petclinic','semantic','主人姓氏查询恰好只有一条结果时，在哪里直接重定向到该主人的详情，而不是渲染列表？请给出数量判断和重定向的证据。',
        [J('owner/OwnerController','if (ownersResults.getTotalElements() == 1)','return "redirect:/owners/" + owner.getId();','ownersResults.getTotalElements() == 1','return "redirect:/owners/"')])
    locate('J3','petclinic','cross_file','表单输入的宠物类型名称怎样转换成数据库中的类型对象？请同时找到名称匹配逻辑，以及为它提供按名称排序类型列表的仓库查询。',
        [J('owner/PetTypeFormatter','public PetType parse','throw new ParseException','Objects.equals(type.getName(), text)'),
         J('owner/PetTypeRepository','@Query("SELECT ptype','List<PetType> findPetTypes();','ORDER BY ptype.name')])
    locate('J6','petclinic','absent','这个源码范围内，基于向量数据库对宠物病历做语义最近邻搜索的实现在哪里？',[])
    locate('T1','agentdemo','exact','定位 routingScore，给出已验证任务历史、阻塞历史和当前负载如何影响最终得分的源码证据。',
        [T('task-routing','export function routingScore','relevanceMethod:', 'const historyScore','- load * 2')])
    locate('T2','agentdemo','semantic','网络请求做 DNS 检查后，怎样避免实际连接时重新解析到另一个地址？请找出固定已校验地址的连接回调，并同时给出所有解析地址都必须是公网地址的判断。',
        [T('network','export async function fetchPublic(', 'const html =', 'addresses.every((a) => publicAddress(a.address))','cb(null, [chosen])')])
    locate('T3','agentdemo','cross_file','记忆查询等待远程向量返回期间，记忆可能被修改或撤销。请给出重新读取并排除旧向量分数的逻辑，以及最终排序前检查有效期和项目作用域的逻辑。',
        [T('memory-index','async recall(', 'fallback,', 'const current = this.store.list','scores.delete(m.id)'),
         T('memory-retrieval','export function recallMemories(', 'const frequency =', 'm.expiresAt > now','m.scope ===')])
    locate('T6','agentdemo','absent','这个源码范围内，内置 Raft 共识协议的选主和持久化日志复制实现在哪里？',[])
    repairs = [
        ('P4','click','bool', 'src/click/types.py', 'value.strip().lower()', 'value.strip()',
         '布尔参数出现回归：带空白的混合大小写真值、假值字符串不再被接受。请修复归一化，保留已有布尔对象、所有合法别名、空字符串和非法输入的原有语义。'),
        ('P5','click','help', 'src/click/utils.py', 'help.partition("\\n\\n")[0].split()', 'help.split()',
         '命令简短帮助会错误地混入空行之后的第二段文字。请恢复只使用第一段的行为，同时保留缩写判断、长度限制、空输入和不重排标记的既有行为。'),
        ('J4','petclinic','owner', JROOT+'owner/Owner.java','compName.equalsIgnoreCase(name)','compName.equals(name)',
         '主人按宠物名字查找时出现大小写敏感回归：已有名字 Milo，却无法用 mILO 找到。请修复，同时保留空名字处理、ignoreNew 对尚未保存宠物的过滤和按编号查找的行为。'),
        ('J5','petclinic','entity', JROOT+'model/BaseEntity.java','return this.id == null;','return this.id != null;',
         '实体的新建状态被判断反了：尚无数据库编号的对象被认为已经持久化，有编号的对象却被认为是新对象。请修复这个共用行为，保持编号 getter/setter 及领域对象继承接口不变。'),
        ('T4','agentdemo','network','server/services/network.ts','a === 127 ||','a === 126 ||',
         '公网地址过滤出现回归：127 段回环地址被接受，126 段普通公网地址反而被拒绝。请修复并保留其他内网、链路本地、共享地址、多播和 IPv6 限制。不需要发起真实网络请求。'),
        ('T5','agentdemo','memory','server/services/memory-retrieval.ts','m.expiresAt > now','m.expiresAt < now',
         '记忆召回错误地选入过期记忆，并漏掉仍在有效期内的记忆。请修复最终召回的有效期判断，同时保持激活状态、用户/项目作用域和相关性过滤不退化。'),
    ]
    for id, source, verifier, path, old, new, query in repairs:
        text = (roots[source]/path).read_text(encoding='utf-8')
        if text.count(old) != 1: raise ValueError((id,'mutation not unique',old))
        rows.append(dict(id=id,source=source,kind='repair',query=query,targets=[],verifier=verifier,
                         mutation=dict(path=path,old=old,new=new)))
    return sorted(rows,key=lambda c:c['id'])
