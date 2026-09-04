; REQUIRES: qsym
; RUN: %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: printf "\0" | env SYMCC_OUTPUT_DIR=%t-out %t
; RUN: %python -c "from pathlib import Path; values=[p.read_bytes() for p in Path(r'%t-out').iterdir() if p.is_file()]; assert any(v[:1] == b'A' for v in values), values"
; RUN: %symcc -O0 -S -emit-llvm %s -o %t.ll 2>%t.err
; RUN: not grep "unknown instruction.*freeze" %t.err

declare i64 @read(i32, ptr, i64)

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %count = call i64 @read(i32 0, ptr %input, i64 1)
  %value = load i8, ptr %input, align 1
  %stable = freeze i8 %value
  %target = icmp eq i8 %stable, 65
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}
