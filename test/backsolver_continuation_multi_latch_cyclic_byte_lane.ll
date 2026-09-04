; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; c=next(x for x in s['exit_states'] if x['state_kind']=='multi-latch-cyclic-byte-composition'); b=c['backedge_states']; assert r['analysis']=='llvm-memoryssa-aa-multi-latch-cyclic-byte-lane-revalidated-v10' and s['state_schema']=='multi-latch-cyclic-byte-lane-continuation-memory-tuple-v10' and [[y['source_kind'] for y in x['lanes']] for x in b]==[['store','carry'],['carry','store']] and len({c['cycle_header_site'],c['cycle_entry_site'],*(x['cycle_backedge_site'] for x in b)})==4"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); c=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='multi-latch-cyclic-byte-composition'); c['backedge_states'][0]['lanes'][0]['source_kind']='carry'; open(r'%t.transfer-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.transfer-tamper
; RUN: rm -f %t.seal %t.unified.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='zext i8 68 to i16'; new='zext i8 69 to i16'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,n: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,n]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',a,n)==run(r'%t.lowered.ll',a,n) for a in range(256) for n in range(4))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: %python -c "import os,subprocess; e=dict(os.environ,SYMCC_OUTPUT_DIR=r'%t-out',SYMCC_AFL_COVERAGE_MAP=r'%t-map',SYMCC_TELEMETRY_OUT=r'%t.json'); subprocess.run([r'%t'],input=b'C\\x02',env=e,check=True)"
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m)>=2 and m[:1]==b'A' and m[1]>=2 for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_loop(i8 %selector, i8 %iterations, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %next.low, %latch.low ],
                  [ %next.high, %latch.high ]
  %done = icmp uge i8 %index, %iterations
  br i1 %done, label %loop.exit, label %body

body:
  %parity = and i8 %index, 1
  %use.low = icmp eq i8 %parity, 0
  br i1 %use.low, label %latch.low, label %latch.high

latch.low:
  store i8 51, ptr %slot, align 1
  %next.low = add i8 %index, 1
  br label %loop

latch.high:
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 68, ptr %high, align 1
  %next.high = add i8 %index, 1
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
  %input = alloca [2 x i8], align 1
  %slot = alloca i16, align 2
  %read = call i64 @read(i32 0, ptr %input, i64 2)
  %selector.ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 0
  %iterations.ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 1
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %choice = call i8 @choose_loop(i8 %selector, i8 %iterations, ptr %slot)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca [2 x i8], align 1
  %slot = alloca i16, align 2
  %read = call i64 @read(i32 0, ptr %input, i64 2)
  %selector.ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 0
  %iterations.ptr = getelementptr inbounds [2 x i8], ptr %input, i64 0, i64 1
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %choice = call i8 @choose_loop(i8 %selector, i8 %iterations, ptr %slot)
  %target = icmp eq i8 %choice, 120
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i8 @choose_loop
; LOWERED-LABEL: loop:
; LOWERED: %ifss.cont.memory.multi.cycle = phi i16 [ %ifss.cont.byte.compose{{[0-9]*}}, %entry ], [ %ifss.cont.byte.compose{{[0-9]*}}, %latch.low ], [ %ifss.cont.byte.compose{{[0-9]*}}, %latch.high ], !symcc.ifss_continuation_memory_multi_cycle ![[MEM:[0-9]+]]
; LOWERED-LABEL: latch.low:
; LOWERED: %ifss.cont.byte.extend{{[0-9]*}} = zext i8 51 to i16
; LOWERED: %ifss.cont.byte.extract{{[0-9]*}} = lshr i16 %ifss.cont.memory.multi.cycle, 8
; LOWERED-LABEL: latch.high:
; LOWERED: %ifss.cont.byte{{[0-9]*}} = trunc i16 %ifss.cont.memory.multi.cycle to i8
; LOWERED: %ifss.cont.byte.extend{{[0-9]*}} = zext i8 68 to i16
; LOWERED: ![[MEM]] = !{!"multi-latch-cyclic-byte-lane-continuation-memory-tuple-v10"

; IFSS-LABEL: define i8 @choose_loop
; IFSS: %ifss.cont.memory.multi.cycle = phi i16
; IFSS: %ifss.cont.memory = phi i16
; IFSS: call ptr @_sym_build_ite
; IFSS: multi-latch-cyclic-byte-lane-continuation-memory-tuple-v10
