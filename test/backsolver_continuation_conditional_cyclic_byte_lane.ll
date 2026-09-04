; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; c=next(x for x in s['exit_states'] if x['state_kind']=='conditional-cyclic-byte-composition'); b=c['backedge_state']; assert r['analysis']=='llvm-memoryssa-aa-conditional-cyclic-byte-lane-revalidated-v9' and s['state_schema']=='conditional-cyclic-byte-lane-continuation-memory-tuple-v9' and [x['source_kind'] for x in b['lanes']]==['carry','guarded-store'] and c['cycle_store_when']=='true' and len({c[k] for k in ('cycle_header_site','cycle_entry_site','cycle_backedge_site','cycle_branch_site','cycle_store_arm_site','cycle_carry_arm_site','cycle_guard_site')})==7"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); c=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='conditional-cyclic-byte-composition'); c['cycle_store_when']='false'; open(r'%t.polarity-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.polarity-tamper
; RUN: rm -f %t.seal %t.unified.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='select i1 %write.guard, i8 51, i8'; new='select i1 %write.guard, i8 52, i8'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,n,w: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,n,w]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',a,n,w)==run(r'%t.lowered.ll',a,n,w) for a in range(256) for n in range(2) for w in range(2))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: %python -c "import os,subprocess; e=dict(os.environ,SYMCC_OUTPUT_DIR=r'%t-out',SYMCC_AFL_COVERAGE_MAP=r'%t-map',SYMCC_TELEMETRY_OUT=r'%t.json'); subprocess.run([r'%t'],input=b'C\\x01\\x01',env=e,check=True)"
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m)>=3 and m[:1]==b'A' and m[1]>=1 and m[2]!=0 for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_loop(i8 %selector, i8 %iterations, i8 %write, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %next, %latch ]
  %done = icmp uge i8 %index, %iterations
  br i1 %done, label %loop.exit, label %body

body:
  %next = add i8 %index, 1
  %write.guard = icmp ne i8 %write, 0
  br i1 %write.guard, label %write.arm, label %carry.arm

write.arm:
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 51, ptr %high, align 1
  br label %latch

carry.arm:
  br label %latch

latch:
  br label %loop

loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
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
  %iterations.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 1
  %write.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 2
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %write = load i8, ptr %write.ptr, align 1
  %choice = call i8 @choose_loop(i8 %selector, i8 %iterations, i8 %write, ptr %slot)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca [3 x i8], align 1
  %slot = alloca i16, align 2
  %read = call i64 @read(i32 0, ptr %input, i64 3)
  %selector.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 0
  %iterations.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 1
  %write.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 2
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %write = load i8, ptr %write.ptr, align 1
  %choice = call i8 @choose_loop(i8 %selector, i8 %iterations, i8 %write, ptr %slot)
  %target = icmp eq i8 %choice, 18
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i8 @choose_loop
; LOWERED-LABEL: loop:
; LOWERED: %ifss.cont.memory.cycle = phi i16 [ %ifss.cont.byte.compose{{[0-9]*}}, %entry ], [ %ifss.cont.byte.compose{{[0-9]*}}, %latch ], !symcc.ifss_continuation_memory_cycle ![[MEM:[0-9]+]]
; LOWERED-LABEL: latch:
; LOWERED: %ifss.cont.byte{{[0-9]*}} = trunc i16 %ifss.cont.memory.cycle to i8
; LOWERED: %ifss.cont.byte.cycle.guard = select i1 %write.guard, i8 51, i8 %ifss.cont.byte
; LOWERED: %ifss.cont.byte.extend{{[0-9]*}} = zext i8 %ifss.cont.byte.cycle.guard to i16
; LOWERED: %ifss.cont.byte.position{{[0-9]*}} = shl i16 %ifss.cont.byte.extend{{[0-9]*}}, 8
; LOWERED-LABEL: ifss.cont.dispatch:
; LOWERED: %ifss.cont.memory = phi i16
; LOWERED: ![[MEM]] = !{!"conditional-cyclic-byte-lane-continuation-memory-tuple-v9"

; IFSS-LABEL: define i8 @choose_loop
; IFSS: %ifss.cont.memory.cycle = phi i16
; IFSS: %ifss.cont.byte.cycle.guard = select i1
; IFSS: %ifss.cont.memory = phi i16
; IFSS: call ptr @_sym_build_ite
; IFSS: conditional-cyclic-byte-lane-continuation-memory-tuple-v9
