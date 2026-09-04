; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 SYMCC_IFSS_LOOP_EXIT_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=SUMMARY
; RUN: %python %S/../util/verify_ifss_loop_exit_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); assert r['state_count']==1 and r['maximum_trip_count']==7 and len(r['execution_table'])==64 and r['execution_table'][26]=={'trip':3,'break_at':2,'break_taken':True,'executions':3}"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['execution_table'][26]['executions']=2; open(r'%t.table-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_loop_exit_manifest.py %t.table-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['states'][0]['break_liveout_site']=r['states'][0]['phi_site']; open(r'%t.liveout-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_loop_exit_manifest.py %t.liveout-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['proof_fingerprint']='1'; open(r'%t.hash-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_loop_exit_manifest.py %t.hash-tamper
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=0 %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.disabled.ll
; RUN: %filecheck %s --input-file=%t.disabled.ll --check-prefix=DISABLED
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.disabled.ll',a,b)==run(r'%t.lowered.ll',a,b) for a in range(8) for b in range(8))"
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf '\000\007' | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['solver_queries'] >= 1 and d['solver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*')]; assert models and any(len(m)>=2 and (m[0]&7)>=3 and (m[1]&7)==2 for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @break_loop(i8 %raw_trip, i8 %raw_break) {
entry:
  %trip = and i8 %raw_trip, 7
  %break_at = and i8 %raw_break, 7
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 10, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i8 %index, %trip
  br i1 %continue, label %latch, label %normal

latch:
  %state_next = add i8 %state, 3
  %index_next = add i8 %index, 1
  %should_break = icmp eq i8 %index, %break_at
  br i1 %should_break, label %break, label %loop

normal:
  %normal_state = phi i8 [ %state, %loop ]
  br label %final

break:
  %break_state = phi i8 [ %state_next, %latch ]
  br label %final

final:
  %state_out = phi i8 [ %normal_state, %normal ],
                       [ %break_state, %break ]
  %exit_bias = phi i8 [ 0, %normal ], [ 100, %break ]
  %result = add i8 %state_out, %exit_bias
  ret i8 %result
}

define i32 @semantic_main() {
entry:
  %input = alloca [2 x i8], align 1
  %pointer = getelementptr [2 x i8], [2 x i8]* %input, i64 0, i64 0
  %read = call i64 @read(i32 0, i8* %pointer, i64 2)
  %raw_trip = load i8, i8* %pointer, align 1
  %next = getelementptr i8, i8* %pointer, i64 1
  %raw_break = load i8, i8* %next, align 1
  %result = call i8 @break_loop(i8 %raw_trip, i8 %raw_break)
  %status = zext i8 %result to i32
  ret i32 %status
}

define i32 @main() {
entry:
  %input = alloca [2 x i8], align 1
  %pointer = getelementptr [2 x i8], [2 x i8]* %input, i64 0, i64 0
  %read = call i64 @read(i32 0, i8* %pointer, i64 2)
  %raw_trip = load i8, i8* %pointer, align 1
  %next = getelementptr i8, i8* %pointer, i64 1
  %raw_break = load i8, i8* %next, align 1
  %result = call i8 @break_loop(i8 %raw_trip, i8 %raw_break)
  %target = icmp eq i8 %result, 119
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)

; SUMMARY-LABEL: define i8 @break_loop
; SUMMARY-LABEL: entry:
; SUMMARY: %ifss.loop.break_taken = icmp ult i8 %break_at, %trip, !symcc.ifss_loop_break ![[BREAK:[0-9]+]]
; SUMMARY-NEXT: %ifss.loop.break_iterations = add i8 %break_at, 1, !symcc.ifss_loop_break ![[BREAK]]
; SUMMARY-NEXT: %ifss.loop.executions = select i1 %ifss.loop.break_taken, i8 %ifss.loop.break_iterations, i8 %trip, !symcc.ifss_loop_break ![[BREAK]]
; SUMMARY: %ifss.loop.delta = mul i8 %ifss.loop.executions, 3
; SUMMARY: br i1 %ifss.loop.break_taken, label %break, label %normal{{.*}}!symcc.ifss_loop_break ![[BREAK]]
; SUMMARY-NOT: loop:
; SUMMARY-NOT: latch:
; SUMMARY-LABEL: normal:
; SUMMARY-NEXT: br label %final
; SUMMARY-LABEL: break:
; SUMMARY-NEXT: br label %final
; SUMMARY-LABEL: final:
; SUMMARY: %state_out = phi i8 [ %ifss.loop.state, %normal ], [ %ifss.loop.state, %break ]
; SUMMARY: ![[BREAK]] = !{!"bounded-break-loop-exit-v1", i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 7, i32 1}

; DISABLED-LABEL: define i8 @break_loop
; DISABLED: loop:
; DISABLED: latch:
; DISABLED: %should_break = icmp eq i8 %index, %break_at
; DISABLED-NOT: bounded-break-loop-exit-v1

; IFSS-LABEL: define i8 @break_loop
; IFSS-NOT: loop:
; IFSS: call ptr @_sym_build_unsigned_less_than
; IFSS: call ptr @_sym_build_ite
; IFSS: !"bounded-break-loop-exit-v1"
