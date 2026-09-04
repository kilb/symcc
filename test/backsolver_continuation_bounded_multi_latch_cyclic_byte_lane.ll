; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; c=next(x for x in s['exit_states'] if x['state_kind']=='bounded-multi-latch-cyclic-byte-composition'); b=c['backedge_states']; sig={(x['transfer_kind'],x.get('cycle_store_when'),tuple(y['source_kind'] for y in x['lanes'])) for x in b}; guards={x['cycle_guard_site'] for x in b if x['transfer_kind']=='conditional'}; assert r['analysis']=='llvm-memoryssa-aa-bounded-multi-latch-cyclic-byte-lane-revalidated-v13' and s['state_schema']=='bounded-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v13' and sig=={('conditional','true',('guarded-store','carry','carry','carry')),('unconditional',None,('carry','store','carry','carry')),('conditional','false',('carry','carry','guarded-store','carry')),('unconditional',None,('carry','carry','carry','store'))} and len(guards)==1"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); c=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='bounded-multi-latch-cyclic-byte-composition'); c['backedge_states'][3]['ordinal']=2; open(r'%t.ordinal-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.ordinal-tamper
; RUN: rm -f %t.seal %t.unified.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='i8 81, i8'; new='i8 82, i8'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,n,w: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,n,w]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',a,n,w)==run(r'%t.lowered.ll',a,n,w) for a in range(256) for n in range(5) for w in range(2))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: %python -c "import os,subprocess; e=dict(os.environ,SYMCC_OUTPUT_DIR=r'%t-out',SYMCC_AFL_COVERAGE_MAP=r'%t-map',SYMCC_TELEMETRY_OUT=r'%t.json'); subprocess.run([r'%t'],input=b'C\\x04\\x01',env=e,check=True)"
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m)>=3 and m[:1]==b'A' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_loop(
    i8 %selector, i8 %iterations, i8 %write, ptr %slot) {
entry:
  store i32 287454020, ptr %slot, align 4
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %next.0, %latch.0 ],
                  [ %next.1, %latch.1 ], [ %next.2, %latch.2 ],
                  [ %next.3, %latch.3 ]
  %done = icmp uge i8 %index, %iterations
  br i1 %done, label %loop.exit, label %body

body:
  %arm = and i8 %index, 3
  %shared.guard = icmp ne i8 %write, 0
  switch i8 %arm, label %latch.3 [
    i8 0, label %branch.0
    i8 1, label %latch.1
    i8 2, label %branch.2
  ]

branch.0:
  br i1 %shared.guard, label %write.0, label %carry.0
write.0:
  store i8 81, ptr %slot, align 1
  br label %latch.0
carry.0:
  br label %latch.0
latch.0:
  %next.0 = add i8 %index, 1
  br label %loop

latch.1:
  %byte.1 = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 98, ptr %byte.1, align 1
  %next.1 = add i8 %index, 1
  br label %loop

branch.2:
  br i1 %shared.guard, label %carry.2, label %write.2
write.2:
  %byte.2 = getelementptr inbounds i8, ptr %slot, i64 2
  store i8 115, ptr %byte.2, align 1
  br label %latch.2
carry.2:
  br label %latch.2
latch.2:
  %next.2 = add i8 %index, 1
  br label %loop

latch.3:
  %byte.3 = getelementptr inbounds i8, ptr %slot, i64 3
  store i8 132, ptr %byte.3, align 1
  %next.3 = add i8 %index, 1
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
  store i32 1432778632, ptr %slot, align 4
  br label %continuation_a
deep:
  store i32 16909060, ptr %slot, align 4
  br label %continuation_b
continuation_a:
  %tag_a = phi i8 [ 1, %left ], [ 2, %middle ]
  %state = load i32, ptr %slot, align 4
  %byte0 = trunc i32 %state to i8
  %shift1 = lshr i32 %state, 8
  %byte1 = trunc i32 %shift1 to i8
  %shift2 = lshr i32 %state, 16
  %byte2 = trunc i32 %shift2 to i8
  %shift3 = lshr i32 %state, 24
  %byte3 = trunc i32 %shift3 to i8
  %xor01 = xor i8 %byte0, %byte1
  %xor012 = xor i8 %xor01, %byte2
  %fold = xor i8 %xor012, %byte3
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
  %slot = alloca i32, align 4
  %read = call i64 @read(i32 0, ptr %input, i64 3)
  %selector.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 0
  %iterations.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 1
  %write.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 2
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %write = load i8, ptr %write.ptr, align 1
  %choice = call i8 @choose_loop(
      i8 %selector, i8 %iterations, i8 %write, ptr %slot)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca [3 x i8], align 1
  %slot = alloca i32, align 4
  %read = call i64 @read(i32 0, ptr %input, i64 3)
  %selector.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 0
  %iterations.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 1
  %write.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 2
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %write = load i8, ptr %write.ptr, align 1
  %choice = call i8 @choose_loop(
      i8 %selector, i8 %iterations, i8 %write, ptr %slot)
  %target = icmp eq i8 %choice, 150
  br i1 %target, label %hit, label %miss
hit:
  ret i32 0
miss:
  ret i32 0
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i8 @choose_loop
; LOWERED-LABEL: loop:
; LOWERED: %ifss.cont.memory.multi.cycle = phi i32
; LOWERED-LABEL: latch.0:
; LOWERED: select i1 %shared.guard, i8 81
; LOWERED-LABEL: latch.1:
; LOWERED: zext i8 98 to i32
; LOWERED-LABEL: latch.2:
; LOWERED: select i1 %shared.guard
; LOWERED-LABEL: latch.3:
; LOWERED: zext i8 -124 to i32
; LOWERED: bounded-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v13

; IFSS-LABEL: define i8 @choose_loop
; IFSS: %ifss.cont.memory.multi.cycle = phi i32
; IFSS: call ptr @_sym_build_ite
; IFSS: bounded-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v13
