; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; c=next(x for x in s['exit_states'] if x['state_kind']=='symbolic-region-multi-latch-cyclic-byte-composition'); b=c['backedge_states']; w=[x['symbolic_region_writer'] for x in b]; assert r['analysis']=='llvm-memoryssa-aa-symbolic-region-multi-latch-cyclic-byte-lane-revalidated-v18' and s['state_schema']=='symbolic-region-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v18' and [x['transfer_kind'] for x in b]==['conditional','unconditional'] and b[0]['cycle_store_when']=='true' and all([y['source_kind'] for y in x['lanes']]==['carry']*4 for x in b) and len({x['store_site'] for x in w})==2 and len({x['index_site'] for x in w})==2 and len({x['region_base_site'] for x in w})==1 and all(x['region_extent']=='8' for x in w)"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); c=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='symbolic-region-multi-latch-cyclic-byte-composition'); c['backedge_states'][1]['symbolic_region_writer']['region_extent']='9'; open(r'%t.extent-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.extent-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); c=next(x for x in r['memory_slots'][0]['exit_states'] if x['state_kind']=='symbolic-region-multi-latch-cyclic-byte-composition'); c['backedge_states'][0]['symbolic_region_writer']['lane_cases'][2]['cases'][0]['source_byte']=1; open(r'%t.lane-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.lane-tamper
; RUN: rm -f %t.seal %t.unified.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='icmp eq i64 %high.index, 2'; new='icmp eq i64 %high.index, 7'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,a,n,w,v: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([a,n,w,v]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',a,n,w,v)==run(r'%t.lowered.ll',a,n,w,v) for a in (64,65,66) for n in range(7) for w in (0,1) for v in (0,17,255))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: %python -c "import os,subprocess; e=dict(os.environ,SYMCC_OUTPUT_DIR=r'%t-out',SYMCC_AFL_COVERAGE_MAP=r'%t-map',SYMCC_TELEMETRY_OUT=r'%t.json'); subprocess.run([r'%t'],input=b'C\\x03\\x01\\xaa',env=e,check=True)"
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m)>=4 and m[0]==65 for m in models)"
; RUN: %python %S/../util/cross_llvm_transform_replay.py verify --certificate %S/../benchmark/evidence/ifss_f294_cross_llvm_certificate.json

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_loop(
    i8 %selector, i8 %iterations, i8 %write, i8 %value) {
entry:
  %slot = call ptr @malloc(i64 8)
  store i32 1144201745, ptr %slot, align 4
  %limit = zext i8 %iterations to i64
  %write.guard = icmp ne i8 %write, 0
  br label %loop

loop:
  %index = phi i64 [ 0, %entry ], [ %next.low, %latch.low ],
                   [ %next.high, %latch.high ]
  %done = icmp uge i64 %index, %limit
  br i1 %done, label %loop.exit, label %body

body:
  %parity = and i64 %index, 1
  %use.low = icmp eq i64 %parity, 0
  br i1 %use.low, label %low.branch, label %high.write

low.branch:
  br i1 %write.guard, label %low.write, label %low.carry

low.write:
  %low.element = getelementptr inbounds i8, ptr %slot, i64 %index
  store i8 %value, ptr %low.element, align 1
  br label %latch.low

low.carry:
  br label %latch.low

latch.low:
  %next.low = add i64 %index, 1
  br label %loop

high.write:
  %high.index = add i64 %index, 1
  %high.element = getelementptr inbounds i8, ptr %slot, i64 %high.index
  store i8 90, ptr %high.element, align 1
  br label %latch.high

latch.high:
  %next.high = add i64 %index, 1
  br label %loop

loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep

left:
  br label %continuation_a

deep:
  store i32 305419896, ptr %slot, align 4
  br label %continuation_b

continuation_a:
  %tag_a = phi i8 [ 1, %left ]
  %state = load i32, ptr %slot, align 4
  %b0 = trunc i32 %state to i8
  %s1 = lshr i32 %state, 8
  %b1 = trunc i32 %s1 to i8
  %s2 = lshr i32 %state, 16
  %b2 = trunc i32 %s2 to i8
  %s3 = lshr i32 %state, 24
  %b3 = trunc i32 %s3 to i8
  %x01 = xor i8 %b0, %b1
  %x23 = xor i8 %b2, %b3
  %fold = xor i8 %x01, %x23
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

declare noalias ptr @malloc(i64)
declare i64 @read(i32, ptr, i64)

define i32 @semantic_main() {
entry:
  %input = alloca [4 x i8], align 1
  %read = call i64 @read(i32 0, ptr %input, i64 4)
  %selector.ptr = getelementptr inbounds [4 x i8], ptr %input, i64 0, i64 0
  %iterations.ptr = getelementptr inbounds [4 x i8], ptr %input, i64 0, i64 1
  %write.ptr = getelementptr inbounds [4 x i8], ptr %input, i64 0, i64 2
  %value.ptr = getelementptr inbounds [4 x i8], ptr %input, i64 0, i64 3
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %write = load i8, ptr %write.ptr, align 1
  %value = load i8, ptr %value.ptr, align 1
  %choice = call i8 @choose_loop(
      i8 %selector, i8 %iterations, i8 %write, i8 %value)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca [4 x i8], align 1
  %read = call i64 @read(i32 0, ptr %input, i64 4)
  %selector.ptr = getelementptr inbounds [4 x i8], ptr %input, i64 0, i64 0
  %iterations.ptr = getelementptr inbounds [4 x i8], ptr %input, i64 0, i64 1
  %write.ptr = getelementptr inbounds [4 x i8], ptr %input, i64 0, i64 2
  %value.ptr = getelementptr inbounds [4 x i8], ptr %input, i64 0, i64 3
  %selector = load i8, ptr %selector.ptr, align 1
  %iterations = load i8, ptr %iterations.ptr, align 1
  %write = load i8, ptr %write.ptr, align 1
  %value = load i8, ptr %value.ptr, align 1
  %choice = call i8 @choose_loop(
      i8 %selector, i8 %iterations, i8 %write, i8 %value)
  %target = icmp eq i8 %choice, 0
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

; LOWERED-LABEL: define i8 @choose_loop
; LOWERED-LABEL: loop:
; LOWERED: %ifss.cont.memory.multi.cycle = phi i32
; LOWERED-LABEL: latch.low:
; LOWERED: %ifss.cont.region.hit = icmp eq i64 %index, 0
; LOWERED: %ifss.cont.byte.region = select i1 %ifss.cont.region.hit, i8 %value
; LOWERED: %ifss.cont.byte.region.guard = select i1 %write.guard
; LOWERED-LABEL: latch.high:
; LOWERED: %ifss.cont.region.hit{{[0-9]*}} = icmp eq i64 %high.index, 0
; LOWERED: %ifss.cont.byte.region{{[0-9]*}} = select i1 %ifss.cont.region.hit{{[0-9]*}}, i8 90
; LOWERED: symbolic-region-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v18

; IFSS-LABEL: define i8 @choose_loop
; IFSS: %ifss.cont.memory.multi.cycle = phi i32
; IFSS: call ptr @_sym_build_equal
; IFSS: call ptr @_sym_build_ite
; IFSS: symbolic-region-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v18
