; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); assert r['exit_count']==3 and r['destination_count']==2 and r['scalar_slot_count']==2 and r['memory_slot_count']==2 and r['analysis']=='llvm-memoryssa-aa-revalidated-v1'"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['exits'][0]['destination']=1; open(r'%t.edge-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.edge-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['memory_slots'][0]['exit_states'][0]['skipped_nomod_sites'][0]='1'; open(r'%t.chain-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.chain-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['proof_fingerprint']='1'; open(r'%t.hash-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.hash-tamper
; RUN: rm -f %t.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py verify --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.seal
; RUN: not %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python -c "open(r'%t.lowered-tamper.ll','wb').write(open(r'%t.lowered.ll','rb').read()+b'\n; tamper\n')"
; RUN: not %python %S/../util/seal_ifss_continuation_artifact.py verify --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered-tamper.ll --compiler %passlib --llvm-tool %opt --seal %t.seal
; RUN: %python -c "import json; r=json.load(open(r'%t.seal')); r['record_count']+=1; open(r'%t.seal-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/seal_ifss_continuation_artifact.py verify --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.seal-tamper
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='store i8 1, ptr %noise'; new='store i8 1, ptr '+chr(37)+'slot_a'; assert p.count(old)==1; open(r'%t.alias-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.alias-tamper.ll
; RUN: rm -f %t.alias-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.alias-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.alias-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.alias-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.alias-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: rm -f %t.structural.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.structural.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.disabled.ll
; RUN: %filecheck %s --input-file=%t.disabled.ll --check-prefix=DISABLED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.structural.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.structural.manifest').read()); assert r['memory_slot_count']==0 and r['analysis']=='structural-only-v1'"
; RUN: rm -f %t.structural.unified.seal
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.structural.manifest --input-ir %s --lowered-ir %t.disabled.ll --compiler %passlib --llvm-tool %opt --output %t.structural.unified.seal
; RUN: %python -c "import json; r=json.load(open(r'%t.structural.unified.seal')); assert r['replay_configuration']['continuation_state'] is True and r['replay_configuration']['continuation_memory'] is False"
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.structural.manifest --input-ir %s --lowered-ir %t.disabled.ll --compiler %passlib --llvm-tool %opt --seal %t.structural.unified.seal
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.disabled.ll',b)==run(r'%t.lowered.ll',b) for b in range(256))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf C | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_attempts'] >= 1; assert d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(m[:1] == b'B' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose(i8 %selector, i8* %slot_a, i8* %slot_b) {
entry:
  %noise = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
  store i8 10, i8* %slot_a, align 1
  store i8 1, i8* %noise, align 1
  br label %continuation_a

rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep

middle:
  store i8 20, i8* %slot_b, align 1
  store i8 2, i8* %noise, align 1
  br label %continuation_b

deep:
  store i8 30, i8* %slot_a, align 1
  store i8 3, i8* %noise, align 1
  br label %continuation_a

continuation_a:
  %a_tag = phi i8 [ 1, %left ], [ 3, %deep ]
  %a_state = load i8, i8* %slot_a, align 1
  %a_result = add i8 %a_state, %a_tag
  br label %final

continuation_b:
  %b_tag = phi i8 [ 2, %middle ]
  %b_state = load i8, i8* %slot_b, align 1
  %b_result = sub i8 %b_state, %b_tag
  br label %final

final:
  %result = phi i8 [ %a_result, %continuation_a ],
                   [ %b_result, %continuation_b ]
  ret i8 %result
}

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %slot_a = alloca i8, align 1
  %slot_b = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %selector = load i8, i8* %input, align 1
  %choice = call i8 @choose(i8 %selector, i8* %slot_a, i8* %slot_b)
  %target = icmp eq i8 %choice, 18
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

define i32 @semantic_main() {
entry:
  %input = alloca i8, align 1
  %slot_a = alloca i8, align 1
  %slot_b = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %selector = load i8, i8* %input, align 1
  %choice = call i8 @choose(i8 %selector, i8* %slot_a, i8* %slot_b)
  %result = zext i8 %choice to i32
  ret i32 %result
}

declare i64 @read(i32, i8*, i64)

; LOWERED-LABEL: define i8 @choose
; LOWERED-LABEL: ifss.cont.dispatch:
; LOWERED: %ifss.cont.exit_id = phi i8
; LOWERED: %ifss.cont.liveout = phi i8
; LOWERED: %ifss.cont.liveout{{[0-9]+}} = phi i8
; LOWERED: %ifss.cont.memory = phi i8 [ 10, %ifss.cont.capture ], [ 0, %ifss.cont.capture{{[0-9]+}} ], [ 30, %ifss.cont.capture{{[0-9]+}} ], !symcc.ifss_continuation_memory ![[MEM0:[0-9]+]]
; LOWERED: %ifss.cont.memory{{[0-9]+}} = phi i8 [ 0, %ifss.cont.capture ], [ 20, %ifss.cont.capture{{[0-9]+}} ], [ 0, %ifss.cont.capture{{[0-9]+}} ], !symcc.ifss_continuation_memory ![[MEM1:[0-9]+]]
; LOWERED-LABEL: continuation_a:
; LOWERED: %a_state = load i8, ptr %slot_a, align 1, {{.*}}!symcc.ifss_continuation_memory_source ![[MEM0]]
; LOWERED: %a_result = add i8 %ifss.cont.memory, %a_tag
; LOWERED-LABEL: continuation_b:
; LOWERED: %b_state = load i8, ptr %slot_b, align 1, {{.*}}!symcc.ifss_continuation_memory_source ![[MEM1]]
; LOWERED: %b_result = sub i8 %ifss.cont.memory{{[0-9]+}}, %b_tag
; LOWERED: ![[MEM0]] = !{!"must-alias-continuation-memory-tuple-v1", i64 {{-?[0-9]+}}, i32 0, i32 0, i64 {{-?[0-9]+}}, i32 3, i32 2, i32 0, i64 {{-?[0-9]+}}, i32 1, i64 {{-?[0-9]+}}, i32 2, i64 {{-?[0-9]+}}, i32 1, i64 {{-?[0-9]+}}}
; LOWERED: ![[MEM1]] = !{!"must-alias-continuation-memory-tuple-v1", i64 {{-?[0-9]+}}, i32 1, i32 1, i64 {{-?[0-9]+}}, i32 3, i32 1, i32 1, i64 {{-?[0-9]+}}, i32 1, i64 {{-?[0-9]+}}}

; IFSS-LABEL: define i8 @choose
; IFSS: %ifss.cont.memory = phi i8
; IFSS: %ifss.cont.memory{{[0-9]+}} = phi i8
; IFSS: call ptr @_sym_build_ite
; IFSS-COUNT-2: must-alias-continuation-memory-tuple-v1
; IFSS: partition-condition-cache-v1

; DISABLED-LABEL: define i8 @choose
; DISABLED-NOT: ifss.cont.memory
; DISABLED-NOT: must-alias-continuation-memory-tuple-v1
