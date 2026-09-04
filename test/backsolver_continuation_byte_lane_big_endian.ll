; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=BIG
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; assert r['analysis']=='llvm-memoryssa-aa-byte-lane-revalidated-v4' and all(x['endianness']=='big' for x in s['exit_states']) and [x['source_kind'] for x in s['exit_states'][1]['lanes']]==['store','live-on-entry']"

target datalayout = "E-m:e-i64:64-n32:64"
target triple = "powerpc64-unknown-linux-gnu"

define i16 @choose_bytes_big(i8 %selector, ptr %slot) {
entry:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
  store i16 4386, ptr %slot, align 2
  %low = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 51, ptr %low, align 1
  br label %continuation_a

rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep

middle:
  store i8 68, ptr %slot, align 1
  br label %continuation_a

deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i16 [ 1, %left ], [ 2, %middle ]
  %state = load i16, ptr %slot, align 2
  %result = add i16 %state, %tag_a
  ret i16 %result

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

; BIG-LABEL: define i16 @choose_bytes_big
; BIG-LABEL: ifss.cont.capture:
; BIG: %ifss.cont.byte.extract = lshr i16 4386, 8
; BIG: %ifss.cont.byte{{[0-9]*}} = trunc i16 %ifss.cont.byte.extract to i8
; BIG: %ifss.cont.byte.extend = zext i8 %ifss.cont.byte{{[0-9]*}} to i16
; BIG: %ifss.cont.byte.position = shl i16 %ifss.cont.byte.extend, 8
; BIG: %ifss.cont.byte.extend{{[0-9]+}} = zext i8 51 to i16
; BIG-LABEL: ifss.cont.capture{{[0-9]+}}:
; BIG: %ifss.cont.byte.extend{{[0-9]+}} = zext i8 68 to i16
; BIG: %ifss.cont.byte.position{{[0-9]+}} = shl i16 %ifss.cont.byte.extend{{[0-9]+}}, 8
; BIG: %ifss.cont.initial = load i16, ptr %slot, align 2
; BIG: byte-lane-continuation-memory-tuple-v4
