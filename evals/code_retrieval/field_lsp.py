"""Real project navigation, including a two-module Java Maven workspace."""
import argparse
from dataclasses import replace
from pathlib import Path
import shutil
import time
from codeagent.lsp import LspService, LspConfig
from codeagent.lsp.registry import POLYGLOT_SERVERS
from evals.evidence import write_json, read_json


def java_modules(root):
    root.mkdir(parents=True)
    header='<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
    (root/'pom.xml').write_text(header+'<groupId>probe</groupId><artifactId>parent</artifactId><version>1</version><packaging>pom</packaging><properties><maven.compiler.source>17</maven.compiler.source><maven.compiler.target>17</maven.compiler.target></properties><modules><module>library</module><module>application</module></modules></project>')
    for name in ('library','application'):
        module=root/name;module.mkdir()
        dependency='<dependencies><dependency><groupId>probe</groupId><artifactId>library</artifactId><version>1</version></dependency></dependencies>' if name=='application' else ''
        (module/'pom.xml').write_text(header+'<parent><groupId>probe</groupId><artifactId>parent</artifactId><version>1</version></parent><artifactId>'+name+'</artifactId>'+dependency+'</project>')
        package=module/'src/main/java/demo';package.mkdir(parents=True)
        if name=='library':(package/'Maths.java').write_text('package demo;\npublic class Maths {\n public static int twice(int x) { return x * 2; }\n}\n')
        else:(package/'Main.java').write_text('package demo;\npublic class Main {\n int value = Maths.twice(3);\n}\n')


def run(base):
    out=base/'real-project-lsp-v1';out.mkdir(exist_ok=False)
    java_modules(out/'java-modules')
    suite=read_json(base/'suite.json')
    ts=Path(next(s['root'] for s in suite['sources'] if s['id']=='agentdemo'))
    cases=[
        ('click',base/'upstream/click','src/click/decorators.py',34,'get_current_context','src/click/globals.py'),
        ('agentdemo',ts,'server/services/memory-index.ts',69,'recallMemories','server/services/memory-retrieval.ts'),
        ('petclinic',base/'upstream/petclinic','src/main/java/org/springframework/samples/petclinic/owner/PetTypeFormatter.java',53,'findPetTypes','src/main/java/org/springframework/samples/petclinic/owner/PetTypeRepository.java'),
        ('java-modules',out/'java-modules','application/src/main/java/demo/Main.java',3,'twice','library/src/main/java/demo/Maths.java'),
    ]
    results=[]
    for name,root,file,line,symbol,expected in cases:
        root=root.resolve();service=LspService(root,LspConfig(timeout_seconds=30))
        processes=[];started=time.monotonic()
        row=dict(name=name,source=str(root),expected=expected)
        try:
            text=(root/file).read_text(encoding='utf-8').splitlines()[line-1]
            # A declaration can reuse the call name (e.g. findPetTypes = types.findPetTypes()).
            character=len(text[:text.rindex(symbol)].encode('utf-16-le'))//2+1
            attempts=[]
            for _ in range(3):
                report=service.execute('definition',file,line,character)
                attempts.append(report)
                if any(item['path']==expected for item in report.get('locations',[])):break
                if report['status']=='unavailable':break
                # Project import may still be running after initialize completed.
                # Keep the same bounded settling policy for all languages.
                time.sleep(1)
            row.update(definition_attempts=attempts,definition_pass=any(item['path']==expected for item in report.get('locations',[])))
            row['references']=service.execute('references',file,line,character)
            row['references_pass']=any(item['path']==file and item['line']==line
                for item in row['references'].get('locations',[]))
            processes=[s['rpc'].process for s in service.sessions.values()]
        except Exception as exc:row.update(error_type=type(exc).__name__)
        finally:service.close()
        row.update(duration_ms=(time.monotonic()-started)*1000,processes_released=all(p.poll() is not None for p in processes))
        results.append(row);write_json(out/'report.json',results)
        print(name,'definition',row.get('definition_pass'),'references',row.get('references_pass'),'released',row['processes_released'],flush=True)


def maven_followup(base):
    """Diagnostic follow-up after default Gradle import failed; never overwrite v1."""
    out=base/'petclinic-maven-v2';out.mkdir(exist_ok=False)
    root=out/'workspace'
    shutil.copytree(base/'upstream/petclinic',root,
        ignore=shutil.ignore_patterns('.git','.gradle','.settings','.project','.classpath','target','build'))
    settings={'java':{'import':{'gradle':{'enabled':False},'maven':{'enabled':True}},
        'configuration':{'maven':{'defaultMojoExecutionAction':'ignore'}}}}
    server=replace(next(s for s in POLYGLOT_SERVERS if s.name=='jdtls'),settings=settings,
        initialization_options={'settings':settings},startup_seconds=90)
    service=LspService(root,LspConfig(servers=(server,),timeout_seconds=30))
    file='src/main/java/org/springframework/samples/petclinic/owner/PetTypeFormatter.java'
    expected='src/main/java/org/springframework/samples/petclinic/owner/PetTypeRepository.java'
    text=(root/file).read_text(encoding='utf-8').splitlines()[52]
    character=len(text[:text.rindex('findPetTypes')].encode('utf-16-le'))//2+1
    row=dict(protocol='Diagnostic follow-up; explicit Maven initialization options, new project copy and LSP data directory.',settings=settings,attempts=[])
    processes=[];started=time.monotonic()
    try:
        for _ in range(3):
            report=service.execute('definition',file,53,character)
            row['attempts'].append(report)
            write_json(out/'report.json',row)
            if any(item['path']==expected for item in report.get('locations',[])):break
            time.sleep(1)
        row['definition_pass']=any(item['path']==expected for item in report.get('locations',[]))
        row['references']=service.execute('references',file,53,character)
        row['references_pass']=any(item['path']==file and item['line']==53 for item in row['references'].get('locations',[]))
    except Exception as exc:row['error_type']=type(exc).__name__
    finally:
        processes=[s['rpc'].process for s in service.sessions.values()]
        service.close()
    row.update(duration_ms=(time.monotonic()-started)*1000,processes_released=all(p.poll() is not None for p in processes))
    write_json(out/'report.json',row)
    print('Petclinic explicit Maven:',row.get('definition_pass'),row.get('references_pass'),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base',type=Path,required=True)
    parser.add_argument('--maven-followup',action='store_true')
    args=parser.parse_args()
    (maven_followup if args.maven_followup else run)(args.base.resolve())
