; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; c=next(x for x in s['exit_states'] if x['state_kind']=='nested-predicate-cyclic-byte-composition'); t=c['backedge_states'][0]; assert r['analysis']=='llvm-memoryssa-aa-nested-predicate-cyclic-byte-lane-revalidated-v14' and s['state_schema']=='nested-predicate-cyclic-byte-lane-continuation-memory-tuple-v14' and t['transfer_kind']=='predicate-tree' and len(t['predicate_nodes'])==3 and len(t['predicate_leaves'])==4 and [[x['source_kind'] for x in y['lanes']] for y in t['predicate_leaves']]==[['store','carry','carry','carry'],['carry','store','carry','carry'],['carry','carry','carry','carry'],['carry','carry','store','carry']]"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); t=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='nested-predicate-cyclic-byte-composition')['backedge_states'][0]; t['predicate_nodes'][1]['true_child']['index']=0; open(r'%t.topology-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.topology-tamper
; RUN: rm -f %t.seal %t.unified.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='zext i8 65 to i32'; new='zext i8 66 to i32'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,n,c: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,n,c]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',a,n,c)==run(r'%t.lowered.ll',a,n,c) for a in range(256) for n in range(5) for c in range(4))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: %python -c "import os,subprocess; e=dict(os.environ,SYMCC_OUTPUT_DIR=r'%t-out',SYMCC_AFL_COVERAGE_MAP=r'%t-map',SYMCC_TELEMETRY_OUT=r'%t.json'); subprocess.run([r'%t'],input=b'C\\x04\\x02',env=e,check=True)"
; RUN: %python -c "import glob,json; d=json.load(open(r'%t.json')); models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert d['backsolver_targets']>=1 and d['backsolver_attempts']>=1 and models and any(len(m)>=3 and m[:1]==b'A' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_loop(
    i8 %selector, i8 %iterations, i8 %choice, ptr %slot) {
entry:
  store i32 287454020, ptr %slot, align 4
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %next, %latch ]
  %done = icmp uge i8 %index, %iterations
  br i1 %done, label %loop.exit, label %root

root:
  %arm = and i8 %choice, 3
  %is.0 = icmp eq i8 %arm, 0
  br i1 %is.0, label %leaf.0, label %node.1

node.1:
  %is.1 = icmp eq i8 %arm, 1
  br i1 %is.1, label %leaf.1, label %node.2

node.2:
  %is.2 = icmp eq i8 %arm, 2
  br i1 %is.2, label %leaf.2, label %leaf.3

leaf.0:
  store i8 65, ptr %slot, align 1
  br label %latch

leaf.1:
  %byte.1 = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 82, ptr %byte.1, align 1
  br label %latch

leaf.2:
  br label %latch

leaf.3:
  %byte.2 = getelementptr inbounds i8, ptr %slot, i64 2
  store i8 99, ptr %byte.2, align 1
  br label %latch

latch:
  %next = add i8 %index, 1
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
  %choice.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 2
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %choice = load i8, ptr %choice.ptr, align 1
  %result = call i8 @choose_loop(
      i8 %selector, i8 %iterations, i8 %choice, ptr %slot)
  %wide = zext i8 %result to i32
  ret i32 %wide
}

define i32 @main() {
entry:
  %input = alloca [3 x i8], align 1
  %slot = alloca i32, align 4
  %read = call i64 @read(i32 0, ptr %input, i64 3)
  %selector.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 0
  %iterations.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 1
  %choice.ptr = getelementptr inbounds [3 x i8], ptr %input, i64 0, i64 2
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %choice = load i8, ptr %choice.ptr, align 1
  %result = call i8 @choose_loop(
      i8 %selector, i8 %iterations, i8 %choice, ptr %slot)
  %target = icmp eq i8 %result, 150
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
; LOWERED-LABEL: leaf.0:
; LOWERED: zext i8 65 to i32
; LOWERED-LABEL: leaf.1:
; LOWERED: zext i8 82 to i32
; LOWERED-LABEL: leaf.2:
; LOWERED: trunc i32 %ifss.cont.memory.multi.cycle to i8
; LOWERED-LABEL: leaf.3:
; LOWERED: zext i8 99 to i32
; LOWERED-LABEL: latch:
; LOWERED: %ifss.cont.memory.predicate.cycle = phi i32
; LOWERED: nested-predicate-cyclic-byte-lane-continuation-memory-tuple-v14

; IFSS-LABEL: define i8 @choose_loop
; IFSS: %ifss.cont.memory.multi.cycle = phi i32
; IFSS: %ifss.cont.memory.predicate.cycle = phi i32
; IFSS: call ptr @_sym_build_ite
; IFSS: nested-predicate-cyclic-byte-lane-continuation-memory-tuple-v14
