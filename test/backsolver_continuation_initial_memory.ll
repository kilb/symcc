; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; assert r['memory_slot_count']==1 and r['analysis']=='llvm-memoryssa-aa-live-on-entry-revalidated-v2' and s['state_schema']=='must-alias-or-live-on-entry-continuation-memory-tuple-v2' and [x['state_kind'] for x in s['exit_states']]==['store','live-on-entry'] and 'store_site' not in s['exit_states'][1] and len(s['exit_states'][1]['skipped_nomod_sites'])==1"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['memory_slots'][0]['exit_states'][1]['state_kind']='store'; open(r'%t.kind-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.kind-tamper
; RUN: rm -f %t.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='[ '+chr(37)+'ifss.cont.initial, '+chr(37)+'ifss.cont.capture1 ]'; new='[ 99, '+chr(37)+'ifss.cont.capture1 ]'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',b)==run(r'%t.lowered.ll',b) for b in range(256))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf C | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(m[:1] == b'B' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_partial(i8 %selector, ptr %slot) {
entry:
  %noise = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
  store i8 10, ptr %slot, align 1
  store i8 1, ptr %noise, align 1
  br label %continuation_a

rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep

middle:
  store i8 2, ptr %noise, align 1
  br label %continuation_a

deep:
  store i8 30, ptr %slot, align 1
  br label %continuation_b

continuation_a:
  %tag_a = phi i8 [ 1, %left ], [ 2, %middle ]
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
  %input = alloca i8, align 1
  %slot = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %selector = load i8, ptr %input, align 1
  %initial = xor i8 %selector, 90
  store i8 %initial, ptr %slot, align 1
  %choice = call i8 @choose_partial(i8 %selector, ptr %slot)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %slot = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %selector = load i8, ptr %input, align 1
  %initial = xor i8 %selector, 90
  store i8 %initial, ptr %slot, align 1
  %choice = call i8 @choose_partial(i8 %selector, ptr %slot)
  %target = icmp eq i8 %choice, 26
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i8 @choose_partial
; LOWERED-LABEL: ifss.cont.capture
; LOWERED: %ifss.cont.initial = load i8, ptr %slot, align 1, !symcc.ifss_continuation_memory_initial ![[MEM:[0-9]+]]
; LOWERED-LABEL: ifss.cont.dispatch:
; LOWERED: %ifss.cont.memory = phi i8 [ 10, %ifss.cont.capture ], [ %ifss.cont.initial, %ifss.cont.capture{{[0-9]+}} ], [ 0, %ifss.cont.capture{{[0-9]+}} ], !symcc.ifss_continuation_memory ![[MEM]]
; LOWERED-LABEL: continuation_a:
; LOWERED: %state = load i8, ptr %slot, align 1, {{.*}}!symcc.ifss_continuation_memory_source ![[MEM]]
; LOWERED: %result_a = add i8 %ifss.cont.memory
; LOWERED: ![[MEM]] = !{!"must-alias-or-live-on-entry-continuation-memory-tuple-v2"

; IFSS-LABEL: define i8 @choose_partial
; IFSS: %ifss.cont.initial = load i8, ptr %slot
; IFSS: %ifss.cont.memory = phi i8
; IFSS: call ptr @_sym_build_ite
; IFSS: must-alias-or-live-on-entry-continuation-memory-tuple-v2
