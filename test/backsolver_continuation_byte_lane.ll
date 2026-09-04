; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 SYMCC_IFSS_CONTINUATION_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_ifss_continuation_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); s=r['memory_slots'][0]; a,b=s['exit_states']; assert r['memory_slot_count']==1 and r['analysis']=='llvm-memoryssa-aa-byte-lane-revalidated-v4' and s['state_schema']=='byte-lane-continuation-memory-tuple-v4' and [x['source_kind'] for x in a['lanes']]==['store','store'] and [x['source_byte'] for x in a['lanes']]==[0,0] and [x['source_width'] for x in a['lanes']]==[2,1] and [x['source_kind'] for x in b['lanes']]==['store','live-on-entry'] and len(a['skipped_nomod_sites'])==1 and len(b['skipped_nomod_sites'])==1"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['memory_slots'][0]['exit_states'][1]['lanes'][1]['source_byte']=0; open(r'%t.lane-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_ifss_continuation_manifest.py %t.lane-tamper
; RUN: rm -f %t.seal %t.unified.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline continuation --manifest continuation=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old='store i8 51, ptr %high, align 1'; new='store i8 52, ptr %high, align 1'; assert p.count(old)==1; open(r'%t.value-tamper.ll','w').write(p.replace(old,new,1))"
; RUN: %opt -passes=verify -disable-output %t.value-tamper.ll
; RUN: rm -f %t.value-tamper.seal
; RUN: %python %S/../util/seal_ifss_continuation_artifact.py seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.value-tamper.seal
; RUN: not %python %S/../util/replay_ifss_continuation_artifact.py --seal %t.value-tamper.seal --manifest %t.manifest --input-ir %s --lowered-ir %t.value-tamper.ll --compiler %passlib --llvm-tool %opt
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',b)==run(r'%t.lowered.ll',b) for b in range(256))"
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf C | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1 and d['backsolver_attempts'] >= 1 and d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(m[:1] == b'B' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose_bytes(i8 %selector, ptr %slot) {
entry:
  %noise = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 51, ptr %high, align 1
  store i8 1, ptr %noise, align 1
  br label %continuation_a

rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep

middle:
  store i8 68, ptr %slot, align 1
  store i8 2, ptr %noise, align 1
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
  %input = alloca i8, align 1
  %slot = alloca i16, align 2
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %selector = load i8, ptr %input, align 1
  store i16 26112, ptr %slot, align 2
  %choice = call i8 @choose_bytes(i8 %selector, ptr %slot)
  %result = zext i8 %choice to i32
  ret i32 %result
}

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %slot = alloca i16, align 2
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %selector = load i8, ptr %input, align 1
  store i16 26112, ptr %slot, align 2
  %choice = call i8 @choose_bytes(i8 %selector, ptr %slot)
  %target = icmp eq i8 %choice, 36
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i8 @choose_bytes
; LOWERED-LABEL: ifss.cont.capture:
; LOWERED: %ifss.cont.byte = trunc i16 4386 to i8
; LOWERED: %ifss.cont.byte.extend = zext i8 %ifss.cont.byte to i16
; LOWERED: %ifss.cont.byte.compose = or i16 0, %ifss.cont.byte.extend
; LOWERED: %ifss.cont.byte.extend{{[0-9]*}} = zext i8 51 to i16
; LOWERED: %ifss.cont.byte.position = shl i16 %ifss.cont.byte.extend{{[0-9]*}}, 8
; LOWERED: %ifss.cont.byte.compose{{[0-9]*}} = or i16 %ifss.cont.byte.compose, %ifss.cont.byte.position
; LOWERED-LABEL: ifss.cont.capture{{[0-9]+}}:
; LOWERED: %ifss.cont.byte.extend{{[0-9]*}} = zext i8 68 to i16
; LOWERED: %ifss.cont.byte.compose{{[0-9]*}} = or i16 0, %ifss.cont.byte.extend{{[0-9]*}}
; LOWERED: %ifss.cont.initial = load i16, ptr %slot, align 2, !symcc.ifss_continuation_memory_initial ![[MEM:[0-9]+]]
; LOWERED: %ifss.cont.byte.extract = lshr i16 %ifss.cont.initial, 8
; LOWERED: %ifss.cont.byte{{[0-9]*}} = trunc i16 %ifss.cont.byte.extract to i8
; LOWERED-LABEL: ifss.cont.dispatch:
; LOWERED: %ifss.cont.memory = phi i16 [ %ifss.cont.byte.compose{{[0-9]*}}, %ifss.cont.capture ], [ %ifss.cont.byte.compose{{[0-9]*}}, %ifss.cont.capture{{[0-9]+}} ], [ 0, %ifss.cont.capture{{[0-9]+}} ], !symcc.ifss_continuation_memory ![[MEM]]
; LOWERED: ![[MEM]] = !{!"byte-lane-continuation-memory-tuple-v4"

; IFSS-LABEL: define i8 @choose_bytes
; IFSS: %ifss.cont.memory = phi i16
; IFSS: call ptr @_sym_build_ite
; IFSS: byte-lane-continuation-memory-tuple-v4
