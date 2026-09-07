; RUN: %symcc -O0 %s -o %t
; RUN: printf '\020' | %t 2>&1 | %filecheck %s

@.done = private unnamed_addr constant [6 x i8] c"done\0A\00", align 1

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %value = load i8, i8* %input, align 1

  %sum = call {i8, i1} @llvm.uadd.with.overflow.i8(i8 %value, i8 250)
  %add_overflow = extractvalue {i8, i1} %sum, 1
  br i1 %add_overflow, label %check_sub, label %finish

check_sub:
  %difference = call {i8, i1} @llvm.usub.with.overflow.i8(i8 %value, i8 20)
  %sub_overflow = extractvalue {i8, i1} %difference, 1
  br i1 %sub_overflow, label %check_mul, label %finish

check_mul:
  ; 16 * 16 = 0x0100: the upper half is nonzero while bit 15 is zero.
  %product = call {i8, i1} @llvm.umul.with.overflow.i8(i8 %value, i8 16)
  %mul_overflow = extractvalue {i8, i1} %product, 1
  br i1 %mul_overflow, label %check_i24, label %finish

check_i24:
  ; Non-power-of-two widths exercise the runtime result packing path.
  %wide = zext i8 %value to i24
  %wide_product = call {i24, i1} @llvm.umul.with.overflow.i24(i24 %wide, i24 1048577)
  %wide_overflow = extractvalue {i24, i1} %wide_product, 1
  br i1 %wide_overflow, label %finish, label %finish

finish:
  ; SIMPLE-COUNT-4: Trying to solve
  ; QSYM-COUNT-6: SMT
  ; ANY: done
  %written = call i64 @write(i32 2, i8* getelementptr inbounds ([6 x i8], [6 x i8]* @.done, i64 0, i64 0), i64 5)
  ret i32 0
}

declare i64 @read(i32, i8* nocapture, i64)
declare i64 @write(i32, i8* nocapture readonly, i64)
declare {i8, i1} @llvm.uadd.with.overflow.i8(i8, i8)
declare {i8, i1} @llvm.usub.with.overflow.i8(i8, i8)
declare {i8, i1} @llvm.umul.with.overflow.i8(i8, i8)
declare {i24, i1} @llvm.umul.with.overflow.i24(i24, i24)
