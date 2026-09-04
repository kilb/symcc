; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry survivor_after_symbolic_free
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 00 --expect-values 11,22 --expect-heap --expect-pointer-union --expect-heap-lifetime-pointer-union --expect-collective-heap-union-initialization
; RUN: not env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.partial.json --entry bad_partial_collective_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.partial.json --expect-rejected "heap load lacks a dominating"
; RUN: not env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.nondominating.json --entry bad_nondominating_collective_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.nondominating.json --expect-rejected "heap load lacks a dominating"

declare ptr @malloc(i64)
declare void @free(ptr)

define i8 @survivor_after_symbolic_free(i1 %choose) {
entry:
  %left = call ptr @malloc(i64 1)
  %right = call ptr @malloc(i64 1)
  store i8 11, ptr %left, align 1
  store i8 22, ptr %right, align 1
  %victim = select i1 %choose, ptr %left, ptr %right
  %survivor = select i1 %choose, ptr %right, ptr %left
  call void @free(ptr %victim)
  %value = load i8, ptr %survivor, align 1
  %is_left = icmp eq i8 %value, 11
  br i1 %is_left, label %left_value, label %right_value

left_value:
  ret i8 11

right_value:
  ret i8 22
}

define i8 @bad_partial_collective_initialization(i1 %choose) {
entry:
  %left = call ptr @malloc(i64 1)
  %right = call ptr @malloc(i64 1)
  store i8 11, ptr %left, align 1
  %selected = select i1 %choose, ptr %left, ptr %right
  %value = load i8, ptr %selected, align 1
  ret i8 %value
}

define i8 @bad_nondominating_collective_initialization(i1 %choose) {
entry:
  %left = call ptr @malloc(i64 1)
  %right = call ptr @malloc(i64 1)
  br i1 %choose, label %initialize_left, label %initialize_right

initialize_left:
  store i8 11, ptr %left, align 1
  br label %merge

initialize_right:
  store i8 22, ptr %right, align 1
  br label %merge

merge:
  ; Each arm initializes the object selected by the opposite guard value.
  %selected = select i1 %choose, ptr %right, ptr %left
  %value = load i8, ptr %selected, align 1
  ret i8 %value
}
