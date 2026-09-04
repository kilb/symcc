; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; a,b=s['exit_states']; g=a['lanes'][1]['guarded_source']; assert r['analysis']=='llvm-memoryssa-aa-guarded-byte-lane-revalidated-v5' and s['state_schema']=='guarded-byte-lane-continuation-memory-tuple-v5' and g['store_when']=='true' and g['source_byte']==0 and g['source_width']==1 and 'guarded_source' not in a['lanes'][0] and all('guarded_source' not in x for x in b['lanes'])"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['memory_slots'][0]['exit_states'][0]['lanes'][1]['guarded_source']['store_when']='false'; open(r'%t.guard-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.guard-tamper
; RUN: rm -f %t.seal %t.unified.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old=', i8 51, i8 %ifss.cont.byte'; new=', i8 52, i8 %ifss.cont.byte'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,g: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,g]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',a,g)==run(r'%t.lowered.ll',a,g) for a in range(256) for g in range(4))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: %python -c "import os,subprocess; e=dict(os.environ,SYMCC_OUTPUT_DIR=r'%t-out',SYMCC_AFL_COVERAGE_MAP=r'%t-map',SYMCC_TELEMETRY_OUT=r'%t.json'); subprocess.run([r'%t'],input=b'C\\x01',env=e,check=True)"
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m)>=2 and m[:1]==b'A' and m[1]==1 for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_guarded(i8 %selector, i8 %alias_key, ptr %slot) {
entry:
  %other = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %alias_guard = icmp eq i8 %alias_key, 1
  %alias = select i1 %alias_guard, ptr %high, ptr %other
  store i8 51, ptr %alias, align 1
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
  %input = alloca [2 x i8], align 1
  %slot = alloca i16, align 2
  %read = call i64 @read(i32 0, ptr %input, i64 2)
  %selector_ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 0
  %alias_ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 1
  %selector = load i8, ptr %selector_ptr, align 1
  %alias_key = load i8, ptr %alias_ptr, align 1
  %choice = call i8 @choose_guarded(i8 %selector, i8 %alias_key, ptr %slot)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca [2 x i8], align 1
  %slot = alloca i16, align 2
  %read = call i64 @read(i32 0, ptr %input, i64 2)
  %selector_ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 0
  %alias_ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 1
  %selector = load i8, ptr %selector_ptr, align 1
  %alias_key = load i8, ptr %alias_ptr, align 1
  %choice = call i8 @choose_guarded(i8 %selector, i8 %alias_key, ptr %slot)
  %target = icmp eq i8 %choice, 18
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i8 @choose_guarded
; LOWERED: %ifss.cont.byte.alias = select i1 %alias_guard, i8 51, i8 %ifss.cont.byte
; LOWERED: %ifss.cont.memory = phi i16
; LOWERED: guarded-byte-lane-continuation-memory-tuple-v5

; IFSS-LABEL: define i8 @choose_guarded
; IFSS: %ifss.cont.byte.alias = select i1 %alias_guard
; IFSS: %ifss.cont.memory = phi i16
; IFSS: call ptr @_sym_build_ite
; IFSS: guarded-byte-lane-continuation-memory-tuple-v5
