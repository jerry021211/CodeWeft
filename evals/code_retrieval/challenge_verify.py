"""Executable public/hidden contracts; never placed inside Agent workspaces.

Java compiles actual domain classes with dependency stubs, not a Spring app.
TypeScript is transpiled for behavioral tests, not a full-project type check.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from codeagent.runtime.data_paths import RuntimeDataPaths


PYTHON_PROGRAM = r'''
import json,sys
from pathlib import Path
sys.dont_write_bytecode=True
root,kind,hidden=Path(sys.argv[1]),sys.argv[2],sys.argv[3]=='1'
sys.path.insert(0,str(root/'src'))
import click
assert Path(click.__file__).is_relative_to(root)
results=[]
def check(name,condition):results.append({'name':name,'ok':bool(condition)})
if kind=='bool':
 from click.types import BoolParamType
 f=BoolParamType.str_to_bool
 check('mixed_case_true',f(' YeS ') is True)
 check('mixed_case_false',f(' OFF\t') is False)
 if hidden:
  for value in ['1','true','on','t','y','yes']:check('true_alias_'+value,f(' '+value.upper()+' ') is True)
  for value in ['0','false','off','f','n','no','']:check('false_alias_'+value,f(' '+value.upper()+' ') is False)
  check('boolean_objects',f(True) is True and f(False) is False)
  check('invalid',f('maybe') is None)
  try:BoolParamType().convert('invalid',None,None)
  except click.BadParameter:check('invalid_raises',True)
  else:check('invalid_raises',False)
else:
 from click.utils import _make_default_short_help as f
 check('first_paragraph',f('alpha beta\n\nsecond paragraph',100)=='alpha beta')
 if hidden:
  check('empty',f('')=='')
  check('sentence',f('First sentence. Another sentence.',100)=='First sentence.')
  check('abbreviation',f('Compare vs. previous output. Another.',100)=='Compare vs. previous output.')
  check('nowrap',f('\b\nalpha\n beta\n\nsecond',100)=='alpha beta')
  check('short_limit',f('long long long',2)=='...')
  check('limit',len(f('alpha beta gamma delta epsilon',15))<=15)
print(json.dumps(results))
'''

NODE_PROGRAM = r'''
const fs=require('fs'),path=require('path');
const [root,out,compiler,kind,hiddenText]=process.argv.slice(2),hidden=hiddenText==='1';
const ts=require(compiler),results=[];
const check=(name,value)=>results.push({name,ok:!!value});
const rels=kind==='network'?['server/services/network.ts','server/core/errors.ts']:['server/services/memory-retrieval.ts'];
for(const rel of rels){
 const r=ts.transpileModule(fs.readFileSync(path.join(root,rel),'utf8'),{fileName:rel,reportDiagnostics:true,compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}});
 if((r.diagnostics||[]).some(d=>d.category===ts.DiagnosticCategory.Error))throw Error('TypeScript syntax error');
 const target=path.join(out,rel.replace(/\.ts$/,'.js'));fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,r.outputText);
}
if(kind==='network'){
 const f=require(path.join(out,'server/services/network.js')).publicAddress;
 check('loopback_rejected',!f('127.0.0.1'));check('public_126_accepted',f('126.1.2.3'));
 if(hidden){for(const ip of ['127.255.2.3','10.1.2.3','172.16.0.1','172.31.1.1','192.168.1.2','169.254.3.4','100.64.1.2','224.0.0.1','::1','fc00::1','::ffff:8.8.8.8','2001:db8::1','not-an-ip'])check('denied_'+ip,!f(ip));
 for(const ip of ['8.8.8.8','1.1.1.1','172.32.0.1','126.255.2.3','2606:4700:4700::1111'])check('allowed_'+ip,f(ip));}
}else{
 const f=require(path.join(out,'server/services/memory-retrieval.js')).recallMemories,now=100000;
 const memory=(id,extra={})=>({id,active:true,expiresAt:null,scope:'user',content:'cats '+id,createdAt:0,revision:1,source:'test',...extra});
 const data=[memory('past',{expiresAt:now-1}),memory('future',{expiresAt:now+1}),memory('forever'),memory('equal',{expiresAt:now})];
 const ids=f(data,'cats',null,now).map(m=>m.id);
 check('expired_excluded',!ids.includes('past'));check('future_included',ids.includes('future'));
 if(hidden){check('no_expiry_included',ids.includes('forever'));check('boundary_excluded',!ids.includes('equal'));
 const scoped=[memory('inactive',{active:false}),memory('own',{scope:'project:A'}),memory('foreign',{scope:'project:B'}),memory('user')];
 const a=f(scoped,'cats','A',now).map(m=>m.id),b=f(scoped,'cats',null,now).map(m=>m.id);
 check('activation',!a.includes('inactive'));check('own_project',a.includes('own'));check('foreign_project',!a.includes('foreign'));
 check('no_project',b.length===1&&b[0]==='user');check('irrelevant',f([memory('x',{content:'dogs'})],'cats',null,now).length===0);}
}
process.stdout.write(JSON.stringify(results));
'''


def java_fixture(root, directory, kind, hidden):
    src=directory/'src';src.mkdir()
    def put(relative,text):
        p=src/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8')
    persistence={
        'Column':'String name() default ""; int length() default 255;',
        'GeneratedValue':'GenerationType strategy() default GenerationType.IDENTITY;',
        'OneToMany':'CascadeType[] cascade() default {}; FetchType fetch() default FetchType.EAGER;',
        'JoinColumn':'String name() default "";', 'OrderBy':'String value() default "";',
        'Table':'String name() default "";', 'Entity':'', 'Id':'', 'MappedSuperclass':''}
    for name,body in persistence.items():put('jakarta/persistence/'+name+'.java','package jakarta.persistence; public @interface '+name+' {'+body+'}')
    for name,values in [('GenerationType','IDENTITY'),('CascadeType','ALL'),('FetchType','EAGER,LAZY')]:
        put('jakarta/persistence/'+name+'.java','package jakarta.persistence; public enum '+name+' {'+values+'}')
    for name,body in [('NotBlank',''),('Size','int max() default 255;'),('Pattern','String regexp(); String message() default "";')]:
        put('jakarta/validation/constraints/'+name+'.java','package jakarta.validation.constraints; public @interface '+name+' {'+body+'}')
    package='org/springframework/samples/petclinic/'
    for name in ['model/BaseEntity','model/Person']+(['owner/Owner'] if kind=='owner' else []):
        put(package+name+'.java',(root/'src/main/java'/package/(name+'.java')).read_text(encoding='utf-8'))
    put('org/springframework/core/style/ToStringCreator.java','package org.springframework.core.style; public class ToStringCreator { public ToStringCreator(Object x){} public ToStringCreator append(String n,Object v){return this;} public String toString(){return "";} }')
    put('org/springframework/util/Assert.java','package org.springframework.util; public class Assert { public static void notNull(Object o,String s){if(o==null)throw new IllegalArgumentException(s);} }')
    put(package+'owner/Visit.java','package org.springframework.samples.petclinic.owner; public class Visit {}')
    put(package+'owner/Pet.java','package org.springframework.samples.petclinic.owner; public class Pet extends org.springframework.samples.petclinic.model.BaseEntity { private String name; public String getName(){return name;} public void setName(String n){name=n;} public void addVisit(Visit v){} }')
    body='''BaseEntity x=new BaseEntity();check("new_without_id",x.isNew());x.setId(1);check("persisted",!x.isNew());'''
    if kind=='entity' and hidden:body+='''x.setId(0);check("zero_id",!x.isNew());x.setId(-1);check("negative_id",!x.isNew());x.setId(null);check("reset_id",x.isNew());Person p=new Person();check("inherited_new",p.isNew());p.setId(7);check("inherited_saved",!p.isNew()&&p.getId()==7);'''
    if kind=='owner':
        body='''Owner o=new Owner();Pet p=new Pet();p.setName("Milo");p.setId(7);o.addPet(p);check("mixed_case",o.getPet("mILO",false)==p);check("upper_case",o.getPet("MILO",true)==p);'''
        if hidden:body+='''check("id_lookup",o.getPet(Integer.valueOf(7))==p);check("missing",o.getPet("unknown",false)==null);check("null_query",o.getPet((String)null,false)==null);Pet u=new Pet();u.setName("NewPet");o.addPet(u);check("include_new",o.getPet("NEWPET",false)==u);check("ignore_new",o.getPet("newpet",true)==null);Pet n=new Pet();o.addPet(n);check("null_name",o.getPet("missing",false)==null);o.addPet(p);check("duplicate_same_object",o.getPets().size()==3);'''
    put('Probe.java','''import java.util.*;import org.springframework.samples.petclinic.model.*;import org.springframework.samples.petclinic.owner.*;
public class Probe {static List<String> results=new ArrayList<>();static void check(String n,boolean ok){results.add("{\\"name\\":\\""+n+"\\",\\"ok\\":"+ok+"}");}
public static void main(String[] args){'''+body+'''System.out.print("["+String.join(",",results)+"]");}}''')
    files=[str(p) for p in src.rglob('*.java')]
    (directory/'classes').mkdir()
    compiled=subprocess.run([shutil.which('javac'),'-encoding','UTF-8','-d',str(directory/'classes'),*files],capture_output=True,timeout=20)
    if compiled.returncode:return dict(ok=False,error='compile_error',details=compiled.stderr.decode(errors='replace')[:1200])
    ran=subprocess.run([shutil.which('java'),'-cp',str(directory/'classes'),'Probe'],capture_output=True,timeout=10)
    return decode(ran)


def decode(result):
    if result.returncode:return dict(ok=False,error='execution_error',details=result.stderr.decode(errors='replace')[-1200:])
    checks=json.loads(result.stdout.decode('utf-8'))
    return dict(ok=bool(checks) and all(c['ok'] for c in checks),checks=checks)


def verify_repair(root:Path, case:dict, *, hidden:bool):
    kind=case['verifier']
    try:
        with tempfile.TemporaryDirectory(prefix='agent-contract-') as temp:
            out=Path(temp)
            if kind in ('bool','help'):
                script=out/'verify.py';script.write_text(PYTHON_PROGRAM,encoding='utf-8')
                result=subprocess.run([sys.executable,'-B',str(script),str(root),kind,str(int(hidden))],capture_output=True,timeout=20)
            elif kind in ('network','memory'):
                compiler=RuntimeDataPaths.default().root/'tooling/typescript-lsp/node_modules/typescript/lib/typescript.js'
                script=out/'verify.cjs';script.write_text(NODE_PROGRAM,encoding='utf-8')
                result=subprocess.run([shutil.which('node'),str(script),str(root),str(out/'compiled'),str(compiler),kind,str(int(hidden))],capture_output=True,timeout=20)
            else:return java_fixture(root,out,kind,hidden)
            return decode(result)
    except Exception as exc:return dict(ok=False,error=type(exc).__name__)
