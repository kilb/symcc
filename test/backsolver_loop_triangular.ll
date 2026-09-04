; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 SYMCC_IFSS_LOOP_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=SUMMARY
; RUN: %python %S/../util/verify_ifss_loop_recurrence_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); assert r['state_count']==3 and r['state_bits']==16 and r['maximum_trip_count']==7 and r['states'][0]['coefficients']==['1','2','0'] and r['states'][1]['coefficients']==['0','1','1'] and r['powers'][7]['trip']==7"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['states'][0]['coefficients'][1]='3'; open(r'%t.matrix-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_loop_recurrence_manifest.py %t.matrix-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['powers'][2]['offset'][0]='0'; open(r'%t.power-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_loop_recurrence_manifest.py %t.power-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['proof_fingerprint']='1'; open(r'%t.hash-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_loop_recurrence_manifest.py %t.hash-tamper
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=0 %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.disabled.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.disabled.ll',b)==run(r'%t.lowered.ll',b) for b in range(32))"
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf '\000' | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['solver_queries'] >= 1 and d['solver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*')]; assert models and any(m and (m[0] & 7) == 5 for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i16 @triangular(i8 %raw, i16 %seed) {
entry:
  %limit = and i8 %raw, 7
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %x = phi i16 [ 1, %entry ], [ %x_next, %latch ]
  %y = phi i16 [ %seed, %entry ], [ %y_next, %latch ]
  %z = phi i16 [ 3, %entry ], [ %z_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %exit

latch:
  %twice_y = mul i16 %y, 2
  %x_linear = add i16 %x, %twice_y
  %x_next = add i16 %x_linear, 3
  %y_next = add i16 %y, %z
  %z_next = add i16 %z, 1
  %index_next = add i8 %index, 1
  br label %loop

exit:
  %xy = add i16 %x, %y
  %result = add i16 %xy, %z
  ret i16 %result
}

define i32 @semantic_main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %raw = load i8, i8* %input, align 1
  %seed = zext i8 %raw to i16
  %result = call i16 @triangular(i8 %raw, i16 %seed)
  %status = zext i16 %result to i32
  ret i32 %status
}

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %raw = load i8, i8* %input, align 1
  %seed = zext i8 %raw to i16
  %result = call i16 @triangular(i8 %raw, i16 %seed)
  %target = icmp eq i16 %result, 184
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)

; SUMMARY-LABEL: define i16 @triangular
; SUMMARY-LABEL: entry:
; SUMMARY: %ifss.loop.trip.wide = zext i8 %limit to i16
; SUMMARY: %ifss.loop.binomial3 = udiv i16 %ifss.loop.binomial.product2, 2
; SUMMARY: %ifss.loop.binomial6 = udiv i16 %ifss.loop.binomial.product5, 3
; SUMMARY: %ifss.loop.initial.term = mul i16 %ifss.loop.binomial, %seed, !symcc.ifss_loop_summary ![[X:[0-9]+]]
; SUMMARY: %ifss.loop.state = add i16
; SUMMARY: br label %exit{{.*}}!symcc.ifss_loop_summary ![[CORE:[0-9]+]]
; SUMMARY-NOT: loop:
; SUMMARY-NOT: latch:
; SUMMARY-LABEL: exit:
; SUMMARY: ret i16
; SUMMARY: ![[X]] = !{!"bounded-upper-triangular-loop-summary-v1", i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 7, i32 3, i32 0, i32 16, i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}}
; SUMMARY: ![[CORE]] = !{!"bounded-upper-triangular-loop-summary-v1", i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 7, i32 3, i32 3, i32 16, i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}}

; IFSS-LABEL: define i16 @triangular
; IFSS-NOT: loop:
; IFSS: call ptr @_sym_build_mul
; IFSS: call ptr @_sym_build_unsigned_div
; IFSS: call ptr @_sym_build_add
; IFSS: !"bounded-upper-triangular-loop-summary-v1"
