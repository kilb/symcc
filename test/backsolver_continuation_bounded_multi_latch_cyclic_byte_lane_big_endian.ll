; RUN: rm -f %t.manifest %t.seal
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=BIG
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; c=next(x for x in s['exit_states'] if x['state_kind']=='bounded-multi-latch-cyclic-byte-composition'); b=c['backedge_states']; lanes={tuple(y['source_kind'] for y in x['lanes']) for x in b}; assert r['analysis']=='llvm-memoryssa-aa-bounded-multi-latch-cyclic-byte-lane-revalidated-v13' and c['endianness']=='big' and [x['transfer_kind'] for x in b]==['unconditional']*3 and lanes=={('carry','carry','carry','store'),('carry','carry','store','carry'),('store','carry','carry','carry')}"
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt

target datalayout = "E-m:e-i64:64-n32:64"
target triple = "powerpc64-unknown-linux-gnu"

define i32 @choose_loop_big(
    i8 %selector, i8 %iterations, ptr %slot) {
entry:
  store i32 287454020, ptr %slot, align 4
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %next.0, %latch.0 ],
                  [ %next.1, %latch.1 ], [ %next.2, %latch.2 ]
  %done = icmp uge i8 %index, %iterations
  br i1 %done, label %loop.exit, label %body
body:
  %arm = urem i8 %index, 3
  switch i8 %arm, label %latch.2 [
    i8 0, label %latch.0
    i8 1, label %latch.1
  ]
latch.0:
  %byte.3 = getelementptr inbounds i8, ptr %slot, i64 3
  store i8 81, ptr %byte.3, align 1
  %next.0 = add i8 %index, 1
  br label %loop
latch.1:
  %byte.2 = getelementptr inbounds i8, ptr %slot, i64 2
  store i8 98, ptr %byte.2, align 1
  %next.1 = add i8 %index, 1
  br label %loop
latch.2:
  store i8 132, ptr %slot, align 1
  %next.2 = add i8 %index, 1
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
  %tag_a = phi i32 [ 1, %left ], [ 2, %middle ]
  %state = load i32, ptr %slot, align 4
  %result_a = add i32 %state, %tag_a
  ret i32 %result_a
continuation_b:
  %tag_b = phi i32 [ 3, %deep ]
  ret i32 %tag_b
}

; BIG-LABEL: define i32 @choose_loop_big
; BIG-LABEL: loop:
; BIG: %ifss.cont.memory.multi.cycle = phi i32
; BIG-LABEL: latch.0:
; BIG: zext i8 81 to i32
; BIG-LABEL: latch.1:
; BIG: zext i8 98 to i32
; BIG-LABEL: latch.2:
; BIG: zext i8 -124 to i32
; BIG: bounded-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v13
