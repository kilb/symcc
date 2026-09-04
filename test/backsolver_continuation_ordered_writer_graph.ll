; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.load(open(r'%t.manifest')); s=r['memory_slots'][0]; x=next(x for x in s['exit_states'] if x['state_kind']=='ordered-writer-graph'); w=x['writer_layers']; old=w[3]['pointer_partition']; leaves=[y['lane_source_bytes'] for y in old['leaves']]; assert r['analysis']=='llvm-memoryssa-aa-ordered-writer-graph-revalidated-v15' and s['state_schema']=='ordered-writer-graph-continuation-memory-tuple-v15' and [y['kind'] for y in w]==['guarded-write','pointer-partition','guarded-write','pointer-partition'] and [y['ordinal'] for y in w]==[0,1,2,3] and all(len(y['pointer_partition']['nodes'])==3 for y in w if y['kind']=='pointer-partition') and all(y[1]==-1 for y in leaves) and any(y[0]==0 for y in leaves)"
; RUN: %python -c "import json; r=json.load(open(r'%t.manifest')); x=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='ordered-writer-graph'); x['writer_layers'][1]['ordinal']=2; open(r'%t.order-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.order-tamper
; RUN: %python -c "import json; r=json.load(open(r'%t.manifest')); x=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='ordered-writer-graph'); g=x['writer_layers'][0]; g['lane_store_when'][1]='none'; open(r'%t.polarity-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.polarity-tamper
; RUN: rm -f %t.seal %t.unified.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='select i1 %new.guard, i8 102, i8 %ifss.cont.byte.partition'; new='select i1 %new.guard, i8 103, i8 %ifss.cont.byte.partition'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import itertools,pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes(b),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; cases=((a,)+bits for a in (64,65,66) for bits in itertools.product(range(2),repeat=6)); assert all(run(r'%t.original.ll',b)==run(r'%t.lowered.ll',b) for b in cases)"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: %python -c "import os,subprocess; e=dict(os.environ,SYMCC_OUTPUT_DIR=r'%t-out',SYMCC_AFL_COVERAGE_MAP=r'%t-map',SYMCC_TELEMETRY_OUT=r'%t.json'); subprocess.run([r'%t'],input=b'C\x01\x01\x01\x01\x01\x01',env=e,check=True)"
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; assert glob.glob(r'%t-out/*-backsolve')"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_writer_graph(
    i8 %selector, i8 %op0, i8 %op1, i8 %og,
    i8 %np0, i8 %np1, i8 %ng, ptr %slot) {
entry:
  %other.op0 = alloca i8, align 1
  %other.op1 = alloca i8, align 1
  %other.og = alloca i8, align 1
  %other.np0 = alloca i8, align 1
  %other.np1 = alloca i8, align 1
  %other.ng = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1

  %old.p0 = icmp eq i8 %op0, 0
  %old.p1 = icmp eq i8 %op1, 0
  %old.pt = select i1 %old.p1, ptr %high, ptr %other.op0
  %old.pf = select i1 %old.p1, ptr %other.op1, ptr %slot
  %old.pointer = select i1 %old.p0, ptr %old.pt, ptr %old.pf
  store i8 51, ptr %old.pointer, align 1
  store i8 119, ptr %high, align 1

  %old.guard = icmp eq i8 %og, 0
  %old.guard.pointer = select i1 %old.guard, ptr %high, ptr %other.og
  store i8 68, ptr %old.guard.pointer, align 1

  %new.p0 = icmp eq i8 %np0, 0
  %new.p1 = icmp eq i8 %np1, 0
  %new.pt = select i1 %new.p1, ptr %high, ptr %other.np0
  %new.pf = select i1 %new.p1, ptr %other.np1, ptr %high
  %new.pointer = select i1 %new.p0, ptr %new.pt, ptr %new.pf
  store i8 85, ptr %new.pointer, align 1

  %new.guard = icmp eq i8 %ng, 0
  %new.guard.pointer = select i1 %new.guard, ptr %high, ptr %other.ng
  store i8 102, ptr %new.guard.pointer, align 1
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
  %input = alloca [7 x i8], align 1
  %slot = alloca i16, align 2
  %read = call i64 @read(i32 0, ptr %input, i64 7)
  %p0 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 0
  %p1 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 1
  %p2 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 2
  %p3 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 3
  %p4 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 4
  %p5 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 5
  %p6 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 6
  %v0 = load i8, ptr %p0, align 1
  %v1 = load i8, ptr %p1, align 1
  %v2 = load i8, ptr %p2, align 1
  %v3 = load i8, ptr %p3, align 1
  %v4 = load i8, ptr %p4, align 1
  %v5 = load i8, ptr %p5, align 1
  %v6 = load i8, ptr %p6, align 1
  %choice = call i8 @choose_writer_graph(
      i8 %v0, i8 %v1, i8 %v2, i8 %v3,
      i8 %v4, i8 %v5, i8 %v6, ptr %slot)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca [7 x i8], align 1
  %slot = alloca i16, align 2
  %read = call i64 @read(i32 0, ptr %input, i64 7)
  %p0 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 0
  %p1 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 1
  %p2 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 2
  %p3 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 3
  %p4 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 4
  %p5 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 5
  %p6 = getelementptr inbounds [7 x i8], ptr %input, i64 0, i64 6
  %v0 = load i8, ptr %p0, align 1
  %v1 = load i8, ptr %p1, align 1
  %v2 = load i8, ptr %p2, align 1
  %v3 = load i8, ptr %p3, align 1
  %v4 = load i8, ptr %p4, align 1
  %v5 = load i8, ptr %p5, align 1
  %v6 = load i8, ptr %p6, align 1
  %choice = call i8 @choose_writer_graph(
      i8 %v0, i8 %v1, i8 %v2, i8 %v3,
      i8 %v4, i8 %v5, i8 %v6, ptr %slot)
  %target = icmp eq i8 %choice, 18
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i8 @choose_writer_graph
; LOWERED: %ifss.cont.byte.writer = select i1 %old.guard, i8 68
; LOWERED: %ifss.cont.byte.partition
; LOWERED: %ifss.cont.byte.writer{{[0-9]+}} = select i1 %new.guard, i8 102
; LOWERED: %ifss.cont.memory = phi i16
; LOWERED: ordered-writer-graph-continuation-memory-tuple-v15

; IFSS-LABEL: define i8 @choose_writer_graph
; IFSS: %ifss.cont.byte.writer = select i1 %old.guard
; IFSS: %ifss.cont.byte.partition
; IFSS: %ifss.cont.byte.writer{{[0-9]+}} = select i1 %new.guard
; IFSS: call ptr @_sym_build_ite
; IFSS: ordered-writer-graph-continuation-memory-tuple-v15
