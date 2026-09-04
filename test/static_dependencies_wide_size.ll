; RUN: rm -f %t.deps
; RUN: env SYMCC_STATIC_DEPENDENCE_OUT=%t.deps %symcc -O0 -c %s -o %t.o
; RUN: grep -q "symcc-static-input-dependence-v1" %t.deps

declare i64 @read(i32, ptr, i128)

define i32 @main() {
entry:
  %buffer = alloca i8, align 1
  %result = call i64 @read(
      i32 0, ptr %buffer, i128 1208925819614629174706176)
  %value = load i8, ptr %buffer, align 1
  %extended = zext i8 %value to i32
  ret i32 %extended
}
