"""Source-reviewed labels, authored before observing retrieval output (not human-certified)."""
from pathlib import Path
import shutil
import subprocess
from codeagent.tools.search_files import search_files
from codeagent.tools.workspace import WorkspaceGuard
from codeagent.code_intelligence.languages import language_for
from evals.evidence import file_hash


def target(path, start, end, *anchors):
    return dict(path=path, start=start, end=end, anchors=list(anchors))


def repository_sources(base):
    specifications = []
    p = lambda file, start, end, *a: target('src/click/' + file + '.py', start, end, *a)
    specifications.append(('click', base / 'upstream/click', ['src/click'], 'python',
        '06b2a678741131fd577ce170e23e5ca0aeba0309', [
        ('P01','exact','Group.resolve_command',[p('core',2116,2140,2119)]),
        ('P02','exact','Choice.normalize_choice',[p('types',406,424,416)]),
        ('P03','chinese','参数指定了多个环境变量时，从哪里依次取第一个非空值？',[p('core',2774,2817,2809,2812)]),
        ('P04','chinese','把标准输入输出作为文件使用时，怎样避免退出上下文后误关闭标准流？',[p('utils',381,427,424,425)]),
        ('P05','chinese','合并在一起的短选项中，有一个需要参数时，剩余字符在哪里被当作该参数？',[p('parser',390,428,409,410)]),
        ('P06','chinese','从当前线程读取命令上下文，没有活动上下文且不是静默模式时在哪里报错？',[p('globals',20,41,36,39)]),
        ('P07','english','Where are enumerated choices normalized with a custom token normalizer and case-insensitive folding?',[p('types',406,424,419,422)]),
        ('P08','english','Where does a standalone double dash stop interpreting subsequent arguments as options?',[p('parser',327,361,333,334)]),
        ('P09','cross_file','定位把当前上下文作为首参数传入回调的装饰器，以及它读取线程上下文栈的实现。',[p('decorators',28,36,34),p('globals',20,41,36)]),
        ('P10','cross_file','打开文件时，对标准流免关闭的外层包装和把短横线文件名解析成标准流的底层实现分别在哪里？',[p('utils',381,427,424,425),p('_compat',374,454,387,391)]),
        ('P11','near_name','Option.resolve_envvar_value 使用自动前缀拼接大写参数名的分支在哪里？',[p('core',3654,3676,3670)]),
        ('P12','near_name','Parameter.get_default 从上下文默认值映射中先取值，再回退本地默认值的实际实现在哪里？',[p('core',2550,2581,2573,2576)]),
        ('P13','absent','在哪里自动生成数据库迁移并回滚 ORM schema？',[]),
        ('P14','absent','内置 OAuth 刷新令牌轮换并持久化加密凭据的实现在哪里？',[]),
    ]))
    j = lambda file, start, end, *a: target('src/main/java/org/springframework/samples/petclinic/owner/' + file + '.java', start, end, *a)
    specifications.append(('petclinic',base/'upstream/petclinic',['src/main'],'java',
        '500158f732419217507c7656904b8e6aa1bcc0d6',[
        ('J01','exact','PetTypeFormatter.parse',[j('PetTypeFormatter',52,60,55)]),
        ('J02','exact','Owner.addPet',[j('Owner',100,113,108)]),
        ('J03','chinese','新增宠物提交时，在哪里拒绝同一主人已有的重名宠物？',[j('PetController',108,137,111,112)]),
        ('J04','chinese','更新主人资料时，怎样阻止表单中的编号和地址路径中的编号不一致？',[j('OwnerController',154,171,161,162)]),
        ('J05','chinese','主人姓氏分页查询如何限制每页五条，并把非法页数至少调整到第一页？',[j('OwnerController',141,146,142,144)]),
        ('J06','chinese','往主人对象添加宠物时如何忽略空值和已经存在的持久化编号？',[j('Owner',100,113,101,108)]),
        ('J07','english','Which formatter displays a placeholder when the pet type name is null?',[j('PetTypeFormatter',46,49,48)]),
        ('J08','english','When editing an existing pet, where is a birth date in the future rejected?',[j('PetController',145,179,159,160)]),
        ('J09','cross_file','从主人按宠物编号添加就诊记录，再到宠物把记录放进集合的两个实现分别在哪里？',[j('Owner',176,186,181,185),j('Pet',81,83,82)]),
        ('J10','cross_file','新增宠物的重复名字检查入口，以及它调用的可忽略未保存宠物的名字查找实现在哪里？',[j('PetController',108,137,111),j('Owner',147,157,150,151)]),
        ('J11','near_name','Owner.getPet 根据 Integer id 查找、跳过尚未保存宠物的重载在哪里？',[j('Owner',129,139,131,133)]),
        ('J12','near_name','Owner.getPet 接受 String name 和 boolean ignoreNew 并忽略名字大小写的重载在哪里？',[j('Owner',147,157,150,151)]),
        ('J13','absent','Stripe 支付退款并校验 webhook 签名的实现在哪里？',[]),
        ('J14','absent','JWT 刷新令牌轮换并拉黑旧令牌的认证实现在哪里？',[]),
    ]))
    t = lambda file,start,end,*a: target('server/services/'+file+'.ts',start,end,*a)
    specifications.append(('agentdemo',Path('D:/ademo/AgentDemo'),['server','shared'],'typescript','workspace-content-hashes',[
        ('T01','exact','validateTaskGraph',[t('task-graph',12,38,30)]),
        ('T02','exact','FileScope.resolve',[t('paths',51,110,99)]),
        ('T03','chinese','网络工具如何判断地址是否属于内网、回环或链路本地地址，并保守限制 IPv6？',[t('network',6,25,14,23)]),
        ('T04','chinese','任务规划提交时在哪里拒绝重复任务编号和依赖环？',[t('task-graph',12,38,26,30)]),
        ('T05','chinese','记忆检索结果在哪里对内容忽略大小写和空白去重，同时限制总字符和最多十二条？',[t('memory-retrieval',14,67,54,64)]),
        ('T06','chinese','崩溃后如何只读取当前文件来确认一次写操作是否已经生效，而不再次执行写入？',[t('recovery',3,27,10,13)]),
        ('T07','english','How are read and write path overlaps detected between two isolated tasks before scheduling?',[t('task-routing',24,47,44,45)]),
        ('T08','english','Where are nested project roots and overlap with private application storage rejected?',[t('paths',9,45,33,38)]),
        ('T09','cross_file','由任务提供和需要的产物推导依赖，再检查任务图是否有环，分别在哪两个文件实现？',[t('task-routing',48,78,66,74),t('task-graph',12,38,30,33)]),
        ('T10','cross_file','文件写入通过临时文件替换，以及恢复时通过完整内容比对确认写入成功，这两个环节分别在哪里？',[t('paths',121,130,126,128),t('recovery',3,27,10)]),
        ('T11','near_name','FileScope.read 读取前检查普通文件和最大字节数的实现在哪里？',[t('paths',111,120,115,119)]),
        ('T12','near_name','FileScope.write 使用独占创建临时文件再重命名的实际实现在哪里？',[t('paths',121,130,125,126,128)]),
        ('T13','absent','内置 Rust Cargo 编译器如何执行增量编译缓存？',[]),
        ('T14','absent','Kubernetes 自定义控制器在哪里持续协调 Deployment 副本？',[]),
    ]))
    result=[]
    for name,source,scopes,language,revision,cases in specifications:
        root=base/'repositories-v2'/name
        root.mkdir(parents=True,exist_ok=False)
        if name == 'agentdemo':
            files=search_files(source,WorkspaceGuard(source))
            if files.incomplete: raise ValueError('Incomplete source inventory')
            paths=files.paths
        else:
            # Upstream archives are under our ignored results directory; they
            # have no Git identity of their own yet. Traverse the declared scope.
            paths=[p for p in source.rglob('*') if p.is_file() and not p.is_symlink()]
        for file in paths:
            relative=file.relative_to(source)
            if not language_for(file) or not any(relative.is_relative_to(scope) for scope in scopes):continue
            to=root/relative;to.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(file,to)
        subprocess.run(['git','init','-q',str(root)],check=True,capture_output=True)
        labels=[]
        for id,category,query,targets in cases:
            for item in targets:
                file=root/item['path']; lines=file.read_text(encoding='utf-8').splitlines()
                if not 1 <= item['start'] <= item['end'] <= len(lines):raise ValueError((id,item))
                if any(not item['start'] <= n <= item['end'] or not lines[n-1].strip() for n in item['anchors']):raise ValueError((id,'invalid anchor'))
                item['source_hash']=file_hash(file)
                item['anchor_text']={str(n):lines[n-1] for n in item['anchors']}
            labels.append(dict(id=id,query=query,category=category,language=language,targets=targets))
        result.append(dict(id=name,root=str(root.resolve()),kind='repository_authored',language=language,
                           source_revision=revision,cases=labels,scope=scopes))
    return result
