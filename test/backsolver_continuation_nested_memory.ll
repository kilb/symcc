; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; a=s['exit_states'][0]; assert r['memory_slot_count']==1 and r['analysis']=='llvm-memoryssa-aa-nested-phi-revalidated-v3' and s['state_schema']=='bounded-acyclic-memoryphi-continuation-memory-tuple-v3' and a['root_node']==0 and [n['state_kind'] for n in a['provenance_nodes']]==['memory-phi','memory-phi','store','store','store'] and len(a['provenance_nodes'][0]['incoming'])==2 and len(a['provenance_nodes'][1]['incoming'])==2"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['memory_slots'][0]['exit_states'][0]['provenance_nodes'][0]['incoming'][0]['node']=2; open(r'%t.edge-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.edge-tamper
; RUN: rm -f %t.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: rm -f %t.unified.seal %t.value-tamper.unified.seal
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/seal_transform_artifact.py verify --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='[ 10, '+chr(37)+'nested.left ]'; new='[ 99, '+chr(37)+'nested.left ]'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.unified.seal
; RUN: not %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --seal %t.value-tamper.unified.seal
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',a,b)==run(r'%t.lowered.ll',a,b) for a in range(256) for b in range(4))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: %python -c "import os,subprocess; e=dict(os.environ,SYMCC_OUTPUT_DIR=r'%t-out',SYMCC_AFL_COVERAGE_MAP=r'%t-map',SYMCC_TELEMETRY_OUT=r'%t.json'); subprocess.run([r'%t'],input=b'C\x03',env=e,check=True)"
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m)>=2 and m[0:1]==b'A' and (m[1]&3)==0 for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_nested(i8 %selector, i8 %inner, ptr %slot) {
entry:
  %noise = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %nested.entry, label %rest

nested.entry:
  %outer_bit = and i8 %inner, 2
  %outer_left = icmp eq i8 %outer_bit, 0
  br i1 %outer_left, label %nested.inner.entry,
      label %nested.outer.right

nested.inner.entry:
  %inner_bit = and i8 %inner, 1
  %inner_left = icmp eq i8 %inner_bit, 0
  br i1 %inner_left, label %nested.left, label %nested.right

nested.left:
  store i8 10, ptr %slot, align 1
  store i8 1, ptr %noise, align 1
  br label %nested.inner.merge

nested.right:
  store i8 20, ptr %slot, align 1
  store i8 2, ptr %noise, align 1
  br label %nested.inner.merge

nested.inner.merge:
  store i8 3, ptr %noise, align 1
  br label %nested.outer.merge

nested.outer.right:
  store i8 25, ptr %slot, align 1
  br label %nested.outer.merge

nested.outer.merge:
  br label %continuation_a

rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep

middle:
  store i8 30, ptr %slot, align 1
  br label %continuation_a

deep:
  store i8 40, ptr %slot, align 1
  br label %continuation_b

continuation_a:
  %tag_a = phi i8 [ 1, %nested.outer.merge ], [ 2, %middle ]
  %state = load i8, ptr %slot, align 1
  %result_a = add i8 %state, %tag_a
  br label %final

continuation_b:
  %tag_b = phi i8 [ 3, %deep ]
  br label %final

final:
  %result = phi i8 [ %result_a, %continuation_a ],
                   [ %tag_b, %continuation_b ]
  ret i8 %result
}

define i32 @semantic_main() {
entry:
  %input = alloca [2 x i8], align 1
  %slot = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 2)
  %selector_ptr = getelementptr [2 x i8], ptr %input, i64 0, i64 0
  %inner_ptr = getelementptr [2 x i8], ptr %input, i64 0, i64 1
  %selector = load i8, ptr %selector_ptr, align 1
  %inner = load i8, ptr %inner_ptr, align 1
  %choice = call i8 @choose_nested(i8 %selector, i8 %inner, ptr %slot)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca [2 x i8], align 1
  %slot = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 2)
  %selector_ptr = getelementptr [2 x i8], ptr %input, i64 0, i64 0
  %inner_ptr = getelementptr [2 x i8], ptr %input, i64 0, i64 1
  %selector = load i8, ptr %selector_ptr, align 1
  %inner = load i8, ptr %inner_ptr, align 1
  %choice = call i8 @choose_nested(i8 %selector, i8 %inner, ptr %slot)
  %target = icmp eq i8 %choice, 11
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i8 @choose_nested
; LOWERED-LABEL: nested.inner.merge:
; LOWERED: %ifss.cont.memory.nested = phi i8 [ 10, %nested.left ], [ 20, %nested.right ], !symcc.ifss_continuation_memory_nested ![[MEM:[0-9]+]]
; LOWERED-LABEL: nested.outer.merge:
; LOWERED: %ifss.cont.memory.nested{{[0-9]*}} = phi i8 [ %ifss.cont.memory.nested, %nested.inner.merge ], [ 25, %nested.outer.right ], !symcc.ifss_continuation_memory_nested ![[MEM]]
; LOWERED-LABEL: ifss.cont.dispatch:
; LOWERED: %ifss.cont.memory = phi i8 [ %ifss.cont.memory.nested{{[0-9]*}}, %ifss.cont.capture ], [ 30, %ifss.cont.capture{{[0-9]+}} ], [ 0, %ifss.cont.capture{{[0-9]+}} ], !symcc.ifss_continuation_memory ![[MEM]]
; LOWERED-LABEL: continuation_a:
; LOWERED: %state = load i8, ptr %slot, align 1, {{.*}}!symcc.ifss_continuation_memory_source ![[MEM]]
; LOWERED: %result_a = add i8 %ifss.cont.memory
; LOWERED: ![[MEM]] = !{!"bounded-acyclic-memoryphi-continuation-memory-tuple-v3"

; IFSS-LABEL: define i8 @choose_nested
; IFSS: %ifss.cont.memory.nested = phi i8
; IFSS: %ifss.cont.memory.nested{{[0-9]*}} = phi i8
; IFSS: %ifss.cont.memory = phi i8
; IFSS: call ptr @_sym_build_ite
; IFSS: bounded-acyclic-memoryphi-continuation-memory-tuple-v3
