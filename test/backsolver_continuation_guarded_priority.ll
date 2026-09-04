; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; a=next(x for x in s['exit_states'] if any(len(l.get('guarded_sources',[]))==2 for l in x['lanes'])); gs=next(l['guarded_sources'] for l in a['lanes'] if len(l.get('guarded_sources',[]))==2); assert r['analysis']=='llvm-memoryssa-aa-guarded-write-priority-revalidated-v8' and s['state_schema']=='guarded-write-priority-continuation-memory-tuple-v8' and [g['priority'] for g in gs]==[0,1] and [g['store_when'] for g in gs]==['true','true']"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); gs=next(l['guarded_sources'] for x in r['memory_slots'][0]['exit_states'] for l in x['lanes'] if len(l.get('guarded_sources',[]))==2); gs[0]['priority']=1; open(r'%t.priority-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.priority-tamper
; RUN: rm -f %t.seal %t.unified.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='select i1 %new.guard, i8 51, i8 %ifss.cont.byte.alias'; new='select i1 %new.guard, i8 52, i8 %ifss.cont.byte.alias'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,o,n: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,o,n]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',a,o,n)==run(r'%t.lowered.ll',a,o,n) for a in range(256) for o in range(2) for n in range(2))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: %python -c "import os,subprocess; e=dict(os.environ,SYMCC_OUTPUT_DIR=r'%t-out',SYMCC_AFL_COVERAGE_MAP=r'%t-map',SYMCC_TELEMETRY_OUT=r'%t.json'); subprocess.run([r'%t'],input=b'C\\x01\\x01',env=e,check=True)"
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m)>=3 and m[:1]==b'A' and m[2]==1 for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_priority(
    i8 %selector, i8 %old.key, i8 %new.key, ptr %slot) {
entry:
  %other0 = alloca i8, align 1
  %other1 = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %old.guard = icmp eq i8 %old.key, 1
  %old.pointer = select i1 %old.guard, ptr %high, ptr %other0
  store i8 34, ptr %old.pointer, align 1
  %new.guard = icmp eq i8 %new.key, 1
  %new.pointer = select i1 %new.guard, ptr %high, ptr %other1
  store i8 51, ptr %new.pointer, align 1
  br label %continuation_a

rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep

middle:
  store i16 17493, ptr %slot, align 2
  br label %continuation_a

deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i8 [ 1, %left ], [ 2, %middle ]
  %state = load i16, ptr %slot, align 2
  %high.word = lshr i16 %state, 8
  %high.byte = trunc i16 %high.word to i8
  %low.byte = trunc i16 %state to i8
  %fold = xor i8 %high.byte, %low.byte
  %result_a = add i8 %fold, %tag_a
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
  %input = alloca [3 x i8], align 1
  %slot = alloca i16, align 2
  %read = call i64 @read(i32 0, ptr %input, i64 3)
  %selector.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 0
  %old.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 1
  %new.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 2
  %selector = load i8, ptr %selector.ptr, align 1
  %old = load i8, ptr %old.ptr, align 1
  %new = load i8, ptr %new.ptr, align 1
  %choice = call i8 @choose_priority(
      i8 %selector, i8 %old, i8 %new, ptr %slot)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca [3 x i8], align 1
  %slot = alloca i16, align 2
  %read = call i64 @read(i32 0, ptr %input, i64 3)
  %selector.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 0
  %old.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 1
  %new.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 2
  %selector = load i8, ptr %selector.ptr, align 1
  %old = load i8, ptr %old.ptr, align 1
  %new = load i8, ptr %new.ptr, align 1
  %choice = call i8 @choose_priority(
      i8 %selector, i8 %old, i8 %new, ptr %slot)
  %target = icmp eq i8 %choice, 18
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i8 @choose_priority
; LOWERED: %ifss.cont.byte.alias = select i1 %old.guard, i8 34
; LOWERED: %ifss.cont.byte.alias{{[0-9]+}} = select i1 %new.guard, i8 51, i8 %ifss.cont.byte.alias
; LOWERED: %ifss.cont.memory = phi i16
; LOWERED: guarded-write-priority-continuation-memory-tuple-v8

; IFSS-LABEL: define i8 @choose_priority
; IFSS: %ifss.cont.byte.alias = select i1 %old.guard
; IFSS: %ifss.cont.byte.alias{{[0-9]+}} = select i1 %new.guard
; IFSS: %ifss.cont.memory = phi i16
; IFSS: call ptr @_sym_build_ite
; IFSS: guarded-write-priority-continuation-memory-tuple-v8
