; REQUIRES: qsym
; RUN: rm -f %t.manifest %t.seal
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=BIG
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; c=next(x for x in s['exit_states'] if x['state_kind']=='ordered-symbolic-region-cyclic-byte-composition'); w=c['backedge_state']['symbolic_region_writers']; cases=lambda x:[[(y['index_value'],y['source_byte']) for y in z['cases']] for z in x['lane_cases']]; expected=[[(0,0),(-1,1)],[(1,0),(0,1)],[(2,0),(1,1)],[(3,0),(2,1)]]; assert r['analysis']=='llvm-memoryssa-aa-ordered-symbolic-region-cyclic-byte-lane-revalidated-v19' and s['state_schema']=='ordered-symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v19' and c['endianness']=='big' and [x['ordinal'] for x in w]==[0,1] and all(x['store_width']==2 and cases(x)==expected for x in w)"
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt

target datalayout = "E-m:e-Fn32-i64:64-n32:64-S128"
target triple = "powerpc64-unknown-linux-gnu"

define i32 @big_endian_ordered_cycle(
    i1 %take, i64 %iterations, i16 %old.value, i16 %new.value) {
entry:
  %slot = call ptr @malloc(i64 8)
  store i32 287454020, ptr %slot, align 4
  br label %loop

loop:
  %index = phi i64 [ 0, %entry ], [ %next, %body ]
  %done = icmp uge i64 %index, %iterations
  br i1 %done, label %loop.exit, label %body

body:
  %old.element = getelementptr inbounds i8, ptr %slot, i64 %index
  store i16 %old.value, ptr %old.element, align 1
  %new.element = getelementptr inbounds i8, ptr %slot, i64 %index
  store i16 %new.value, ptr %new.element, align 1
  %next = add i64 %index, 1
  br label %loop

loop.exit:
  br i1 %take, label %left, label %right
left:
  br label %continuation_a
right:
  store i32 1432778632, ptr %slot, align 4
  br label %continuation_b
continuation_a:
  %tag_a = phi i32 [ 1, %left ]
  %state = load i32, ptr %slot, align 4
  %result_a = xor i32 %state, %tag_a
  br label %final
continuation_b:
  %tag_b = phi i32 [ 2, %right ]
  br label %final
final:
  %result = phi i32 [ %result_a, %continuation_a ],
                    [ %tag_b, %continuation_b ]
  ret i32 %result
}

declare noalias ptr @malloc(i64)

; BIG-LABEL: define i32 @big_endian_ordered_cycle
; BIG-LABEL: body:
; BIG: %ifss.cont.region.hit{{[0-9]*}} = icmp eq i64 %index, -1
; BIG: %ifss.cont.byte.extract{{[0-9]*}} = lshr i16 %old.value, 8
; BIG: %ifss.cont.byte.extract{{[0-9]*}} = lshr i16 %new.value, 8
; BIG: ordered-symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v19
