; RUN: env SYMCC_LIVE_PROGRAM_OUT=%t.json SYMCC_LIVE_ENTRY=check %symcc -O1 -c %s -o %t.o
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 41 --expect-values 0,7,66
; RUN: env SYMCC_LIVE_PROGRAM_OUT=%t.reject.json SYMCC_LIVE_ENTRY=bad %symcc -O1 -c %s -o %t.bad.o
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.reject.json --expect-rejected load
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.cli.json --entry check
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.cli.json --input-hex 41 --expect-values 0,7,66
; RUN: %python %S/../util/symcc_live_state.py %t.store --page-size 64 run-llvm %s --entry check --input-hex 41 --max-steps 512 | FileCheck %s --check-prefix=CLI
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.phi.json --entry phi_swap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.phi.json --expect-values 2
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.recursive.json --entry recursive
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.recursive.json --expect-rejected recursive
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pruned.json --entry pruned
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pruned.json --expect-values 9
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.initial.json --entry memory_initial
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.initial.json --expect-values 287454020
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.memory.json --entry memory_roundtrip
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory.json --input-hex 01000000 --expect-values 68
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.memory-branch.json --entry memory_branch
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-branch.json --input-hex 01000000 --expect-values 0,1
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.readonly.json --entry bad_readonly
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.readonly.json --expect-rejected constant
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.symbolic-gep.json --entry symbolic_gep
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.symbolic-gep.json --input-hex 00 --expect-values 65 --expect-alias
; RUN: env SYMCC_LIVE_INPUT_BUFFER_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.buffer.json --entry buffer_check
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.buffer.json --input-hex 414243 --expect-values 0,66 --expect-input-buffer
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.buffer.json --input-hex 41 --expect-values 9 --expect-input-buffer
; RUN: env SYMCC_LIVE_INPUT_BUFFER_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.buffer-oob.json --entry bad_buffer_read
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.buffer-oob.json --input-hex 41 --expect-runtime-error "outside declared objects" --expect-input-buffer
; RUN: env SYMCC_LIVE_INPUT_BUFFER_LIMIT=16 %python %S/../util/symcc_live_state.py %t.buffer.store --page-size 64 run-llvm %s --entry buffer_check --input-hex 414243 --max-steps 512 | FileCheck %s --check-prefix=BUFFER-CLI
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.stack-calls.json --entry stack_calls
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.stack-calls.json --input-hex 01 --expect-values 6 --expect-stack
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.stack-branch.json --entry stack_branch
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.stack-branch.json --input-hex 01000000 --expect-values 0,1 --expect-stack
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.stack-uninitialized.json --entry bad_stack_uninitialized
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.stack-uninitialized.json --expect-rejected "dominating full-width"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.stack-dynamic.json --entry bad_dynamic_stack
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.stack-dynamic.json --expect-rejected "fixed nonzero"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.heap.json --entry heap_roundtrip
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap.json --input-hex 05000000 --expect-values 12 --expect-heap
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.heap-branch.json --entry heap_branch
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-branch.json --input-hex 01000000 --expect-values 0,1 --expect-heap
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=2 %python %S/../util/llvm_to_continuation.py %s --output %t.heap-pool.json --entry heap_multi_instance
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-pool.json --expect-values 21 --expect-heap-pool --expect-pointer-union
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=2 %python %S/../util/llvm_to_continuation.py %s --output %t.heap-pool-exhausted.json --entry bad_heap_pool_exhaustion
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-pool-exhausted.json --expect-runtime-error "pool is exhausted" --expect-heap-pool
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.heap-uaf.json --entry bad_heap_uaf
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-uaf.json --expect-runtime-error "inactive heap" --expect-heap
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.heap-double-free.json --entry bad_heap_double_free
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-double-free.json --expect-runtime-error "not allocated" --expect-heap
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.heap-uninitialized.json --entry bad_heap_uninitialized
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-uninitialized.json --expect-rejected "heap load lacks a dominating"
; RUN: env SYMCC_LIVE_HEAP_OBJECT_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.heap-dynamic.json --entry dynamic_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-dynamic.json --input-hex 0100000000000000 --expect-values 0,1 --expect-nullable-heap --expect-pointer-union
; RUN: env SYMCC_LIVE_DYNAMIC_HEAP_LIMIT=8 %python %S/../util/llvm_to_continuation.py %s --output %t.heap-calloc.json --entry calloc_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-calloc.json --input-hex 0200000000000000 --expect-values 0,1 --expect-nullable-heap --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.heap-realloc.json --entry realloc_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-realloc.json --input-hex 0200000000000000 --expect-values 1,52 --expect-heap-pool --expect-pointer-union
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.heap-select-free.json --entry heap_select_free
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-select-free.json --input-hex 00 --expect-values 11 --expect-heap --expect-heap-lifetime-pointer-union
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.heap-phi-free.json --entry heap_phi_free
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-phi-free.json --input-hex 00 --expect-values 31,42 --expect-heap --expect-heap-lifetime-pointer-union
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.heap-null-free.json --entry heap_null_free
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-null-free.json --input-hex 00 --expect-values 7 --expect-heap --expect-heap-lifetime-pointer-union
; RUN: not env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.heap-mixed-free.json --entry bad_heap_mixed_free
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-mixed-free.json --expect-rejected "supported heap object base"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.heap-interior-free.json --entry bad_heap_interior_free
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-interior-free.json --expect-rejected "heap object base"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.symbolic-store.json --entry symbolic_store
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.symbolic-store.json --input-hex 00 --expect-values 0,1 --expect-alias
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.heap-alias.json --entry heap_symbolic_index
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap-alias.json --input-hex 00 --expect-values 7 --expect-heap --expect-alias
; RUN: not env SYMCC_LIVE_ALIAS_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.alias-wide.json --entry bad_wide_alias
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.alias-wide.json --expect-rejected "alias set exceeds"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.stack-alias.json --entry stack_symbolic_index
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.stack-alias.json --input-hex 00 --expect-values 7 --expect-stack --expect-alias
; RUN: env SYMCC_LIVE_INPUT_BUFFER_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.buffer-alias.json --entry buffer_symbolic_index
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.buffer-alias.json --input-hex 0107 --expect-values 7 --expect-input-buffer --expect-alias
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.alias-multiterm.json --entry bad_multiterm_alias
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.alias-multiterm.json --expect-rejected "one bounded symbolic offset"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.alias-nested.json --entry nested_symbolic_gep
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.alias-nested.json --input-hex 00 --expect-values 66 --expect-alias
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.alias-inbounds-chain.json --entry nested_inbounds_gep
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.alias-inbounds-chain.json --input-hex 02 --expect-values 65 --expect-alias --expect-alias-index-range 2:4
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.alias-inbounds-base.json --entry nested_inbounds_base
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.alias-inbounds-base.json --input-hex 02 --expect-values 65 --expect-alias --expect-alias-index-range 2:4
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-select.json --entry pointer_select
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-select.json --input-hex 00 --expect-values 1,2 --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-phi.json --entry pointer_phi
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-phi.json --input-hex 00 --expect-values 1,2 --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-store.json --entry pointer_select_store
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-store.json --input-hex 00 --expect-values 0,1 --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-dynamic.json --entry pointer_select_dynamic
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-dynamic.json --input-hex 0000 --expect-values 0,1 --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-gep.json --entry pointer_select_gep
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-gep.json --input-hex 00 --expect-values 0,1 --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.cross-pointer-arg.json --entry cross_pointer_arg
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.cross-pointer-arg.json --input-hex 00 --expect-values 0,1 --expect-pointer-union --expect-cross-function-pointer
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.cross-pointer-echo.json --entry cross_pointer_echo
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.cross-pointer-echo.json --input-hex 00 --expect-values 0,1 --expect-pointer-union --expect-cross-function-pointer
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.cross-pointer-stack.json --entry cross_stack_pointer
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.cross-pointer-stack.json --input-hex 07 --expect-values 7 --expect-stack --expect-cross-function-pointer
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=2 %python %S/../util/llvm_to_continuation.py %s --output %t.cross-pointer-return.json --entry cross_pointer_return
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.cross-pointer-return.json --expect-values 7 --expect-heap-pool --expect-cross-function-pointer
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.cross-pointer-stack-escape.json --entry bad_cross_stack_escape
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.cross-pointer-stack-escape.json --expect-rejected "escapes the callee stack"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.cross-pointer-symbolic.json --entry cross_symbolic_pointer
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.cross-pointer-symbolic.json --input-hex 00 --expect-values 66 --expect-pointer-union --expect-cross-function-pointer
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.indirect-select.json --entry indirect_select
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.indirect-select.json --input-hex 0005 --expect-values 6,7 --expect-indirect-call
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.indirect-phi.json --entry indirect_phi
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.indirect-phi.json --input-hex 0005 --expect-values 6,7 --expect-indirect-call
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.indirect-pointer-return.json --entry indirect_pointer_return
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.indirect-pointer-return.json --input-hex 00 --expect-values 65,66 --expect-indirect-call --expect-pointer-union --expect-cross-function-pointer
; RUN: env SYMCC_LIVE_INPUT_BUFFER_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.memory-compare.json --entry memory_compare_entry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-compare.json --input-hex 414243 --expect-values 0,1 --expect-input-buffer --expect-external-summary
; RUN: env SYMCC_LIVE_INPUT_BUFFER_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.memory-compare-order.json --entry memory_compare_order
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-compare-order.json --input-hex 414243 --expect-values 0,1,2 --expect-input-buffer --expect-external-summary
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.memory-compare-zero.json --entry memory_compare_zero
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-compare-zero.json --expect-values 0 --expect-external-summary
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.memory-compare-union.json --entry memory_compare_union
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-compare-union.json --input-hex 00 --expect-values 0,1 --expect-external-summary --expect-pointer-union
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.memory-compare-wide.json --entry bad_memory_compare_wide
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-compare-wide.json --expect-rejected "at most 64 bytes"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.region-memmove.json --entry region_memmove
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.region-memmove.json --expect-values 1128415553 --expect-region-summary
; RUN: env SYMCC_LIVE_INPUT_BUFFER_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.region-memset.json --entry region_memset
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.region-memset.json --input-hex 4142 --expect-values 23130 --expect-input-buffer --expect-region-summary
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.region-symbolic-set.json --entry region_symbolic_set
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.region-symbolic-set.json --input-hex 5a --expect-values 0,1 --expect-region-summary
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.region-memcpy.json --entry region_memcpy
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.region-memcpy.json --expect-values 1145258561 --expect-region-summary
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.region-zero.json --entry region_zero
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.region-zero.json --expect-values 1
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.region-overlap.json --entry bad_region_memcpy_overlap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.region-overlap.json --expect-rejected "cannot prove source and destination disjoint"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-length.json --entry string_length
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-length.json --input-hex 00 --expect-values 0,1 --expect-string-summary --expect-pointer-union
; RUN: env SYMCC_LIVE_INPUT_BUFFER_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.string-input.json --entry string_input
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-input.json --input-hex 4100 --expect-values 0 --expect-string-summary --expect-input-buffer
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-input.json --input-hex 4142 --expect-values 0 --expect-string-summary --expect-input-buffer
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-compare.json --entry string_compare
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-compare.json --input-hex 00 --expect-values 0,1 --expect-string-summary --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-n-compare.json --entry string_n_compare
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-n-compare.json --expect-values 0 --expect-string-summary
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-n-zero.json --entry string_n_zero
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-n-zero.json --expect-values 0 --expect-external-summary
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.string-n-symbolic.json --entry bad_string_n_symbolic
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-n-symbolic.json --expect-rejected "constant length at most 64"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.memory-search.json --entry memory_search
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-search.json --expect-values 67 --expect-pointer-search --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.memory-search-symbolic.json --entry memory_search_symbolic
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-search-symbolic.json --input-hex 00 --expect-values 0,1 --expect-pointer-search
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.memory-search-zero.json --entry memory_search_zero
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-search-zero.json --expect-values 1 --expect-pointer-search
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.memory-search-wide.json --entry bad_memory_search_wide
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.memory-search-wide.json --expect-rejected "exceeds every finite object extent"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-search.json --entry string_search
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-search.json --expect-values 66 --expect-pointer-search --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-search-union.json --entry string_search_union
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-search-union.json --input-hex 00 --expect-values 0,1 --expect-pointer-search --expect-pointer-union --expect-string-summary
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-copy.json --entry string_copy
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-copy.json --expect-values 1509966401 --expect-string-copy
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-copy-return.json --entry string_copy_return
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-copy-return.json --expect-values 1 --expect-string-copy
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-copy-union.json --entry string_copy_union
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-copy-union.json --input-hex 00 --expect-values 66,88 --expect-string-copy --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-n-copy.json --entry string_n_copy
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-n-copy.json --expect-values 16961 --expect-string-copy
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.string-n-copy-zero.json --entry string_n_copy_zero
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-n-copy-zero.json --expect-values 1 --expect-string-copy
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.string-copy-overlap.json --entry bad_string_copy_overlap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-copy-overlap.json --expect-rejected "cannot prove source and destination objects distinct"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.string-n-copy-symbolic.json --entry bad_string_n_copy_symbolic
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.string-n-copy-symbolic.json --expect-rejected "constant length at most 64"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.defined-div.json --entry defined_div
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.defined-div.json --input-hex 03 --expect-values 0,1 --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.defined-shift.json --entry defined_symbolic_shift
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.defined-shift.json --input-hex 00 --expect-values 0,1 --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.good-flags.json --entry good_integer_flags
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.good-flags.json --expect-values 16 --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-div-zero.json --entry bad_div_zero
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-div-zero.json --expect-infeasible --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-sdiv-overflow.json --entry bad_sdiv_overflow
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-sdiv-overflow.json --expect-infeasible --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-nuw.json --entry bad_nuw_add
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-nuw.json --expect-infeasible --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-nsw.json --entry bad_nsw_mul
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-nsw.json --expect-infeasible --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-exact-div.json --entry bad_exact_div
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-exact-div.json --expect-infeasible --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-exact-shift.json --entry bad_exact_shift
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-exact-shift.json --expect-infeasible --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-poison.json --entry freeze_poison
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-poison.json --expect-values 1,2 --expect-nondeterministic-freeze
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-undef.json --entry freeze_undef
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-undef.json --expect-values 3,4 --expect-nondeterministic-freeze
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.scalar-abs.json --entry scalar_abs
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.scalar-abs.json --input-hex 00000000 --expect-values 1,2 --expect-scalar-summary --expect-external-summary --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.scalar-byte-order.json --entry scalar_byte_order
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.scalar-byte-order.json --expect-values 67305985 --expect-scalar-summary --expect-external-summary
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.scalar-bswap.json --entry scalar_bswap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.scalar-bswap.json --expect-values 67305985 --expect-scalar-summary
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.scalar-bad.json --entry bad_scalar_signature
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.scalar-bad.json --expect-rejected "scalar external has an unsupported signature"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pure-external.json --entry declarative_pure_add
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pure-external.json --input-hex 03000000 --expect-values 1,2 --expect-declarative-pure-external
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pure-external-constant.json --entry declarative_pure_constant
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pure-external-constant.json --expect-values 42 --expect-declarative-pure-external
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pure-external-invoke.json --entry declarative_pure_invoke
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pure-external-invoke.json --expect-values 5 --expect-declarative-pure-external --expect-nounwind-invoke
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.pure-external-impure.json --entry bad_declarative_pure_contract
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pure-external-impure.json --expect-rejected "declarative pure external lacks its total-purity contract"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.pure-external-model.json --entry bad_declarative_pure_model
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pure-external-model.json --expect-rejected "declarative pure external model is invalid"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.pure-external-abi.json --entry bad_declarative_pure_abi
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pure-external-abi.json --expect-rejected "declarative pure external model does not match its integer ABI"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.pure-external-name.json --entry bad_declarative_pure_name
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pure-external-name.json --expect-rejected "declarative pure external function name is invalid"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.pure-external-empty-part.json --entry bad_declarative_pure_empty_part
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pure-external-empty-part.json --expect-rejected "declarative pure external model is invalid"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.pure-external-zero-site.json --entry bad_declarative_pure_zero_site
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pure-external-zero-site.json --expect-rejected "declarative pure external stable site is invalid"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.pure-external-duplicate-site.json --entry bad_declarative_pure_duplicate_site
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pure-external-duplicate-site.json --expect-rejected "declarative pure external stable site collides"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.ctpop.json --entry bitcount_population
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.ctpop.json --input-hex 03 --expect-values 1,2 --expect-bitcount-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.ctlz-zero.json --entry bitcount_leading_zero
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.ctlz-zero.json --expect-values 8 --expect-bitcount-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.cttz.json --entry bitcount_trailing
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.cttz.json --input-hex 08 --expect-values 3,4 --expect-bitcount-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.ctlz-poison.json --entry bad_bitcount_zero_poison
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.ctlz-poison.json --expect-infeasible --expect-bitcount-intrinsic --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-wrapped.json --entry freeze_wrapped_add
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-wrapped.json --expect-values 1,2 --expect-deferred-poison-freeze
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-defined.json --entry freeze_defined_add
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-defined.json --expect-values 2 --expect-deferred-poison-freeze
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-multiple-use.json --entry bad_deferred_poison_multiple_use
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-multiple-use.json --expect-infeasible --expect-ub-guards --reject-capability bounded-multiconsumer-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-multiple-sinks.json --entry freeze_deferred_poison_multiple_sinks
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-multiple-sinks.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-multiconsumer-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-transitive-cast.json --entry freeze_transitive_cast
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-transitive-cast.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-transitive-compare.json --entry freeze_transitive_compare
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-transitive-compare.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-transitive-defined.json --entry freeze_transitive_defined
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-transitive-defined.json --expect-values 3 --expect-deferred-poison-freeze --expect-transitive-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-divzero.json --entry freeze_deferred_division_zero
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-divzero.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-sdiv-overflow.json --entry freeze_deferred_sdiv_overflow
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-sdiv-overflow.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-exact-div.json --entry freeze_deferred_exact_division
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-exact-div.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-defined-div.json --entry freeze_deferred_defined_division
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-defined-div.json --expect-values 4 --expect-deferred-poison-freeze --expect-transitive-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-select-unselected.json --entry freeze_select_unselected_poison
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-select-unselected.json --expect-values 7 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-select-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-select-selected.json --entry freeze_select_selected_poison
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-select-selected.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-select-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-select-condition.json --entry freeze_select_poison_condition
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-select-condition.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-select-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-phi-unselected.json --entry freeze_phi_unselected_poison
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-phi-unselected.json --expect-values 7 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-phi-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-phi-selected.json --entry freeze_phi_selected_poison
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-phi-selected.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-phi-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-poison.json --entry freeze_memory_poison
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-poison.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-defined.json --entry freeze_memory_defined
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-defined.json --expect-values 2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-byte-lane.json --entry freeze_memory_byte_lane_composition
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-byte-lane.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-canonical-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-byte-lane-memory-definedness
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-byte-lane-initial.json --entry freeze_memory_byte_lane_initial_composition
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-byte-lane-initial.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-byte-lane-memory-definedness
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-byte-lane-defined.json --entry freeze_memory_byte_lane_defined_composition
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-byte-lane-defined.json --expect-values 1026 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-byte-lane-memory-definedness
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-byte-lane-overwrite.json --entry freeze_memory_byte_lane_overwrite_composition
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-byte-lane-overwrite.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-byte-lane-memory-definedness --expect-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-byte-lane-phi.json --entry freeze_memory_byte_lane_phi
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-byte-lane-phi.json --input-hex 00 --expect-values 1,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-byte-lane-memory-definedness-phi --expect-byte-lane-phi-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-byte-lane-writer-graph-cross-function.json --entry freeze_memory_byte_lane_writer_graph_cross_function
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-byte-lane-writer-graph-cross-function.json --input-hex 00 --expect-values 2,3,3,3,4,4 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-byte-lane-memory-definedness --expect-byte-lane-writer-graph --expect-byte-lane-memory-definedness-phi --expect-byte-lane-phi-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-byte-lane-upstream-merge.json --entry bad_freeze_memory_byte_lane_upstream_merge
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-byte-lane-upstream-merge.json --input-hex 0000 --expect-infeasible --expect-ub-guards --reject-capability bounded-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-byte-lane-initial-phi.json --entry freeze_memory_byte_lane_initial_phi
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-byte-lane-initial-phi.json --input-hex 00 --expect-values 1,1,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-byte-lane-memory-definedness-phi --expect-byte-lane-phi-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-cyclic-byte-lane.json --entry freeze_memory_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-cyclic-byte-lane.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-canonical-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-cyclic-byte-lane-memory-definedness-phi --expect-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-cyclic-byte-lane-alias.json --entry bad_freeze_memory_cyclic_byte_lane_alias
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-cyclic-byte-lane-alias.json --expect-infeasible --expect-ub-guards --reject-capability bounded-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-conditional-cyclic-byte-lane.json --entry freeze_memory_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-conditional-cyclic-byte-lane-memory-definedness-phi --expect-conditional-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_conditional_cyclic_byte_lane_alias
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-conditional-cyclic-byte-lane-memory-definedness-phi --expect-conditional-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-multiarm-conditional-cyclic-byte-lane.json --entry freeze_memory_multiarm_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-multiarm-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi --expect-multiarm-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-fourarm-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_fourarm_conditional_cyclic_byte_lane
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-fourarm-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-multiarm-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_multiarm_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-multiarm-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi --expect-multiarm-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-multiarm-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_multiarm_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-multiarm-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --expect-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_recursive_conditional_cyclic_byte_lane_shared_leaf
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --expect-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_recursive_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-grouped-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_grouped_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-grouped-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-grouped-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_grouped_recursive_conditional_cyclic_byte_lane_two_groups
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-grouped-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_repeated_source_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-repeated-source-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_repeated_source_recursive_conditional_cyclic_byte_lane_mismatched_override
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-repeated-source-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_composed_repeated_source_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_composed_repeated_source_recursive_conditional_cyclic_byte_lane_shadowed_group
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,1,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_shared_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-multigroup-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_multigroup_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-multigroup-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-multigroup-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_multigroup_recursive_conditional_cyclic_byte_lane_nested_groups
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-multigroup-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-mixed-multigroup-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_mixed_multigroup_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-mixed-multigroup-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,1,2,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-mixed-multigroup-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_mixed_multigroup_recursive_conditional_cyclic_byte_lane_shadowed_group
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-mixed-multigroup-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-grouped-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_grouped_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-grouped-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-grouped-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_grouped_recursive_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-grouped-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_repeated_source_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-repeated-source-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_repeated_source_recursive_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-repeated-source-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_composed_repeated_source_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_composed_repeated_source_recursive_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-multigroup-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_multigroup_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-multigroup-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-multigroup-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_multigroup_recursive_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-multigroup-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-mixed-multigroup-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_mixed_multigroup_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-mixed-multigroup-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,1,2,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-mixed-multigroup-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_mixed_multigroup_recursive_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-mixed-multigroup-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,1,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-trigroup-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_trigroup_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-trigroup-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,1,1,2,2,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-trigroup-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_trigroup_recursive_conditional_cyclic_byte_lane_nested_groups
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-trigroup-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-double-composed-multigroup-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-double-composed-multigroup-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,1,1,2,2,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-double-composed-multigroup-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_same_group
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-double-composed-multigroup-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,1,1,2,2,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-trigroup-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_trigroup_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-trigroup-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,1,1,2,2,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-trigroup-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_trigroup_recursive_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-trigroup-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-composed-trigroup-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_composed_trigroup_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-composed-trigroup-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,1,1,1,2,2,2,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-composed-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-composed-trigroup-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_composed_trigroup_recursive_conditional_cyclic_byte_lane_full_shadow
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-composed-trigroup-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-composed-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-composed-trigroup-recursive-conditional-cyclic-byte-lane.json --entry freeze_memory_forwarded_composed_trigroup_recursive_conditional_cyclic_byte_lane_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-composed-trigroup-recursive-conditional-cyclic-byte-lane.json --input-hex 00 --expect-values 1,1,1,1,1,1,1,1,2,2,2,2,2,2,2 --expect-deferred-poison-freeze --expect-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-forwarded-composed-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi --reject-capability bounded-recursive-cyclic-byte-lane-writer-graph
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bad-freeze-memory-forwarded-composed-trigroup-recursive-conditional-cyclic-byte-lane.json --entry bad_freeze_memory_forwarded_composed_trigroup_recursive_conditional_cyclic_byte_lane_nested
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bad-freeze-memory-forwarded-composed-trigroup-recursive-conditional-cyclic-byte-lane.json --expect-infeasible --expect-ub-guards --reject-capability bounded-forwarded-composed-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-canonical.json --entry freeze_memory_canonical_address
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-canonical.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-canonical-memory-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-cross-block.json --entry freeze_memory_cross_block
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-cross-block.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-cross-block-merge.json --entry freeze_memory_cross_block_initial_merge
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-cross-block-merge.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-initial-memory-definedness-merge
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-cross-function-poison.json --entry freeze_cross_function_poison
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-cross-function-poison.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-cross-function-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-cross-function-defined.json --entry freeze_cross_function_defined
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-cross-function-defined.json --expect-values 2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-cross-function-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-cross-function-multi-call.json --entry bad_freeze_cross_function_multiple_calls
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-cross-function-multi-call.json --expect-infeasible --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-cross-function-argument.json --entry freeze_cross_function_argument
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-cross-function-argument.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-cross-function-argument-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-cross-function-argument-defined.json --entry freeze_cross_function_argument_defined
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-cross-function-argument-defined.json --expect-values 2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-cross-function-argument-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-cross-function-argument-multi-call.json --entry freeze_cross_function_argument_multiple_calls
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-cross-function-argument-multi-call.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-cross-function-argument-poison --expect-multicallsite-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-cross-function-return-multi-call.json --entry freeze_cross_function_return_multiple_calls
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-cross-function-return-multi-call.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-cross-function-deferred-poison --expect-multicallsite-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-cross-function-passthrough.json --entry freeze_cross_function_argument_return_passthrough
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-cross-function-passthrough.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-cross-function-deferred-poison --expect-cross-function-argument-poison --expect-multicallsite-deferred-poison --expect-transitive-call-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-cross-function-uncomposed.json --entry freeze_cross_function_uncomposed_poison
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-cross-function-uncomposed.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-cross-function-deferred-poison --expect-cross-function-argument-poison --reject-capability bounded-transitive-call-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-distinct.json --entry bad_freeze_memory_distinct_address
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-distinct.json --expect-infeasible --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-multi-load.json --entry bad_freeze_memory_multi_load
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-multi-load.json --expect-infeasible --expect-ub-guards --reject-capability bounded-multiaccess-memory-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-multi-sink.json --entry freeze_memory_multiple_load_sinks
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-multi-sink.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-multiaccess-memory-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-diamond.json --entry freeze_memory_diamond
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-diamond.json --input-hex 00 --expect-values 1,1,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-mixed-clobber.json --entry freeze_memory_mixed_clobber
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-mixed-clobber.json --input-hex 00 --expect-values 1,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multilevel-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-initial-definition.json --entry freeze_memory_initial_definition
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-initial-definition.json --input-hex 00 --expect-values 1,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-initial-memory-definedness-merge
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-initial-subobject.json --entry freeze_memory_initial_subobject
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-initial-subobject.json --input-hex 00 --expect-values 1,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-canonical-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-initial-memory-definedness-merge --expect-initial-subobject-definedness-merge
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-definedness-phi.json --entry freeze_memory_definedness_phi
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-definedness-phi.json --input-hex 00 --expect-values 1,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-argument-phi.json --entry freeze_memory_argument_phi
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-argument-phi.json --input-hex 00 --expect-values 1,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-cross-function-argument-poison --expect-interprocedural-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-return-phi.json --entry freeze_memory_return_phi
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-return-phi.json --input-hex 00 --expect-values 1,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-cross-function-deferred-poison --expect-interprocedural-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-multilevel-phi.json --entry freeze_memory_multilevel_phi
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-multilevel-phi.json --input-hex 00 --expect-values 1,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multilevel-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-cyclic-phi.json --entry freeze_memory_cyclic_phi
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-cyclic-phi.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-cyclic-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-cycle-carry.json --entry freeze_memory_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-cycle-carry.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-conditional-cycle-carry.json --entry freeze_memory_conditional_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-conditional-cycle-carry.json --input-hex 00 --expect-values 1,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-forwarded-conditional-cycle-carry.json --entry freeze_memory_forwarded_conditional_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-forwarded-conditional-cycle-carry.json --input-hex 00 --expect-values 1,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multilevel-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry --expect-forwarded-conditional-memory-definedness-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-multiarm-conditional-cycle-carry.json --entry freeze_memory_multiarm_conditional_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-multiarm-conditional-cycle-carry.json --input-hex 0000 --expect-values 1,2,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multilevel-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry --expect-forwarded-conditional-memory-definedness-carry --expect-multiarm-conditional-memory-definedness-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-equivalent-defined-store-cycle-carry.json --entry freeze_memory_equivalent_defined_store_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-equivalent-defined-store-cycle-carry.json --input-hex 0000 --expect-values 1,2,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multilevel-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry --expect-forwarded-conditional-memory-definedness-carry --expect-multiarm-conditional-memory-definedness-carry --expect-equivalent-defined-store-memory-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-mixed-defined-poison-store-cycle-carry.json --entry bad_freeze_memory_mixed_defined_poison_store_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-mixed-defined-poison-store-cycle-carry.json --input-hex 0000 --expect-infeasible --expect-ub-guards --reject-capability bounded-equivalent-defined-store-memory-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-shared-poison-store-cycle-carry.json --entry freeze_memory_shared_poison_store_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-shared-poison-store-cycle-carry.json --input-hex 0000 --expect-values 1,1,2,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multilevel-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry --expect-forwarded-conditional-memory-definedness-carry --expect-multiarm-conditional-memory-definedness-carry --expect-shared-poison-store-memory-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-nested-poison-store-cycle-carry.json --entry freeze_memory_nested_poison_store_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-nested-poison-store-cycle-carry.json --input-hex 0000 --expect-values 1,1,2,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multilevel-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry --expect-forwarded-conditional-memory-definedness-carry --expect-multiarm-conditional-memory-definedness-carry --expect-nested-conditional-memory-definedness-carry --reject-capability bounded-shared-poison-store-memory-carry --reject-capability bounded-recursive-memory-definedness-condition-tree --reject-capability bounded-grouped-recursive-memory-definedness-condition-tree
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-recursive-poison-store-cycle-carry.json --entry freeze_memory_recursive_poison_store_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-recursive-poison-store-cycle-carry.json --input-hex 000000 --expect-values 1,1,1,2,2,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multilevel-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry --expect-forwarded-conditional-memory-definedness-carry --expect-multiarm-conditional-memory-definedness-carry --expect-nested-conditional-memory-definedness-carry --expect-recursive-memory-definedness-condition-tree --reject-capability bounded-shared-poison-store-memory-carry --reject-capability bounded-grouped-recursive-memory-definedness-condition-tree
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-grouped-recursive-poison-store-cycle-carry.json --entry freeze_memory_grouped_recursive_poison_store_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-grouped-recursive-poison-store-cycle-carry.json --input-hex 00000000 --expect-values 1,1,1,1,2,2,2,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multilevel-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry --expect-forwarded-conditional-memory-definedness-carry --expect-multiarm-conditional-memory-definedness-carry --expect-nested-conditional-memory-definedness-carry --expect-recursive-memory-definedness-condition-tree --expect-grouped-recursive-memory-definedness-condition-tree --reject-capability bounded-shared-poison-store-memory-carry --reject-capability bounded-repeated-source-recursive-memory-definedness-condition-tree
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-repeated-source-recursive-poison-store-cycle-carry.json --entry freeze_memory_repeated_source_recursive_poison_store_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-repeated-source-recursive-poison-store-cycle-carry.json --input-hex 00000000 --expect-values 1,1,1,1,2,2,2,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multilevel-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry --expect-forwarded-conditional-memory-definedness-carry --expect-multiarm-conditional-memory-definedness-carry --expect-nested-conditional-memory-definedness-carry --expect-recursive-memory-definedness-condition-tree --expect-repeated-source-recursive-memory-definedness-condition-tree --reject-capability bounded-grouped-recursive-memory-definedness-condition-tree --reject-capability bounded-multicarry-recursive-memory-definedness-condition-tree
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-multicarry-recursive-poison-store-cycle.json --entry freeze_memory_multicarry_recursive_poison_store_cycle
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-multicarry-recursive-poison-store-cycle.json --input-hex 00000000 --expect-values 1,1,1,2,2,2,2,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multilevel-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry --expect-forwarded-conditional-memory-definedness-carry --expect-multiarm-conditional-memory-definedness-carry --expect-nested-conditional-memory-definedness-carry --expect-recursive-memory-definedness-condition-tree --expect-repeated-source-recursive-memory-definedness-condition-tree --expect-multicarry-recursive-memory-definedness-condition-tree --reject-capability bounded-grouped-recursive-memory-definedness-condition-tree
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-multicell-cycle-carry.json --entry freeze_memory_multicell_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-multicell-cycle-carry.json --input-hex 00 --expect-values 1,1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multicell-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-identified-object-multicell-cycle-carry.json --entry freeze_memory_identified_object_multicell_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-identified-object-multicell-cycle-carry.json --input-hex 00 --expect-values 1,1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multicell-memory-definedness-phi --expect-identified-object-multicell-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-fixed-heap-object-multicell-cycle-carry.json --entry freeze_memory_fixed_heap_object_multicell_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-fixed-heap-object-multicell-cycle-carry.json --input-hex 00 --expect-values 1,1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multicell-memory-definedness-phi --expect-multicell-alias-graph --expect-fixed-heap-object-multicell-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-dynamic-heap-object-multicell-cycle-carry.json --entry bad_freeze_memory_dynamic_heap_object_multicell_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-dynamic-heap-object-multicell-cycle-carry.json --input-hex 00 --expect-infeasible --expect-ub-guards --reject-capability bounded-multicell-memory-definedness-phi --reject-capability bounded-fixed-heap-object-multicell-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-finite-pointer-domain-multicell-cycle-carry.json --entry freeze_memory_finite_pointer_domain_multicell_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-finite-pointer-domain-multicell-cycle-carry.json --input-hex 00 --expect-values 1,1,2 --expect-pointer-union --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multicell-memory-definedness-phi --expect-finite-pointer-domain-multicell-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-finite-pointer-phi-domain-multicell-cycle-carry.json --entry freeze_memory_finite_pointer_phi_domain_multicell_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-finite-pointer-phi-domain-multicell-cycle-carry.json --input-hex 00 --expect-values 1,1,2 --expect-pointer-union --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-memory-deferred-poison --expect-cross-block-memory-deferred-poison --expect-branch-memory-deferred-poison --expect-memory-definedness-phi --expect-multicell-memory-definedness-phi --expect-finite-pointer-domain-multicell-memory-definedness-phi --expect-cyclic-memory-definedness-phi --expect-cyclic-memory-definedness-carry --expect-conditional-memory-definedness-carry
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-guard-correlated-pointer-domain-multicell-cycle-carry.json --entry freeze_memory_guard_correlated_pointer_domain_multicell_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-guard-correlated-pointer-domain-multicell-cycle-carry.json --input-hex 00 --expect-values 1,1,2 --expect-pointer-union --expect-deferred-poison-freeze --expect-memory-definedness-phi --expect-multicell-memory-definedness-phi --expect-multicell-alias-graph --expect-guard-correlated-pointer-domain-multicell-memory-definedness-phi --reject-capability bounded-finite-pointer-domain-multicell-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-compatible-overlap-pointer-domain-cycle-carry.json --entry bad_freeze_memory_compatible_overlap_pointer_domain_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-compatible-overlap-pointer-domain-cycle-carry.json --input-hex 00 --expect-infeasible --expect-pointer-union --expect-ub-guards --reject-capability bounded-multicell-memory-definedness-phi --reject-capability bounded-guard-correlated-pointer-domain-multicell-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-phi-correlated-pointer-domain-cycle-carry.json --entry freeze_memory_phi_correlated_pointer_domain_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-phi-correlated-pointer-domain-cycle-carry.json --input-hex 00 --expect-values 1,1,2 --expect-pointer-union --expect-deferred-poison-freeze --expect-memory-definedness-phi --expect-multicell-memory-definedness-phi --expect-multicell-alias-graph --expect-shared-phi-edge-discriminator --expect-phi-correlated-pointer-domain-multicell-memory-definedness-phi --reject-capability bounded-finite-pointer-domain-multicell-memory-definedness-phi --reject-capability bounded-guard-correlated-pointer-domain-multicell-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-phi-correlated-reordered-cycle-carry.json --entry freeze_memory_phi_correlated_reordered_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-phi-correlated-reordered-cycle-carry.json --input-hex 00 --expect-values 1,1,2 --expect-pointer-union --expect-deferred-poison-freeze --expect-memory-definedness-phi --expect-multicell-memory-definedness-phi --expect-multicell-alias-graph --expect-shared-phi-edge-discriminator --expect-phi-correlated-pointer-domain-multicell-memory-definedness-phi --reject-capability bounded-finite-pointer-domain-multicell-memory-definedness-phi --reject-capability bounded-guard-correlated-pointer-domain-multicell-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-compatible-overlap-pointer-phi-domain-cycle-carry.json --entry bad_freeze_memory_compatible_overlap_pointer_phi_domain_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-compatible-overlap-pointer-phi-domain-cycle-carry.json --input-hex 00 --expect-infeasible --expect-pointer-union --expect-ub-guards --reject-capability bounded-multicell-memory-definedness-phi --reject-capability bounded-phi-correlated-pointer-domain-multicell-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-symbolic-index-interval-cycle-carry.json --entry freeze_memory_symbolic_index_interval_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-symbolic-index-interval-cycle-carry.json --input-hex 00 --expect-values 1,1,2 --expect-alias --expect-deferred-poison-freeze --expect-memory-definedness-phi --expect-multicell-memory-definedness-phi --expect-multicell-alias-graph --expect-symbolic-index-interval-multicell-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-overlap-symbolic-index-interval-cycle-carry.json --entry bad_freeze_memory_overlap_symbolic_index_interval_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-overlap-symbolic-index-interval-cycle-carry.json --input-hex 00 --expect-infeasible --expect-alias --expect-ub-guards --reject-capability bounded-multicell-memory-definedness-phi --reject-capability bounded-symbolic-index-interval-multicell-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-same-cell-double-cycle-carry.json --entry freeze_memory_same_cell_double_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-same-cell-double-cycle-carry.json --input-hex 00 --expect-values 1,1,2 --expect-deferred-poison-freeze --expect-memory-definedness-phi --reject-capability bounded-multicell-memory-definedness-phi --reject-capability bounded-identified-object-multicell-memory-definedness-phi --reject-capability bounded-fixed-heap-object-multicell-memory-definedness-phi
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.freeze-memory-three-store-cycle-carry.json --entry bad_freeze_memory_three_store_cycle_carry
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.freeze-memory-three-store-cycle-carry.json --input-hex 000000 --expect-values 2 --expect-ub-guards --reject-capability bounded-nested-conditional-memory-definedness-carry --reject-capability bounded-recursive-memory-definedness-condition-tree
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.bitreverse.json --entry bit_permutation_reverse
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bitreverse.json --input-hex b6 --expect-values 1,2 --expect-bit-permutation-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.fshl.json --entry bit_permutation_fshl
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.fshl.json --expect-values 128 --expect-bit-permutation-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.fshr.json --entry bit_permutation_fshr
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.fshr.json --expect-values 1 --expect-bit-permutation-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.saturating-pack.json --entry saturating_pack
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.saturating-pack.json --expect-values 2155806975 --expect-saturating-arithmetic-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.saturating-symbolic.json --entry saturating_symbolic
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.saturating-symbolic.json --input-hex fa --expect-values 1,2 --expect-saturating-arithmetic-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.saturating-shift.json --entry saturating_shift_pack
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.saturating-shift.json --expect-values 65408 --expect-saturating-arithmetic-intrinsic --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.saturating-shift-poison.json --entry bad_saturating_shift
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.saturating-shift-poison.json --expect-infeasible --expect-saturating-arithmetic-intrinsic --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.overflow-pack.json --entry overflow_arithmetic_pack
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.overflow-pack.json --expect-values 1 --expect-overflow-arithmetic-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.overflow-symbolic.json --entry overflow_arithmetic_symbolic
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.overflow-symbolic.json --input-hex fa --expect-values 1,2 --expect-overflow-arithmetic-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.overflow-minmul.json --entry overflow_signed_minimum_multiply
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.overflow-minmul.json --expect-values 1 --expect-overflow-arithmetic-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.scalar-selection-pack.json --entry scalar_selection_pack
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.scalar-selection-pack.json --expect-values 3277009879045 --expect-scalar-selection-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.scalar-selection-symbolic.json --entry scalar_selection_symbolic
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.scalar-selection-symbolic.json --input-hex 05 --expect-values 1,2 --expect-scalar-selection-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.scalar-abs-poison.json --entry bad_scalar_abs_poison
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.scalar-abs-poison.json --expect-infeasible --expect-scalar-selection-intrinsic --expect-ub-guards
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.optimization-hints.json --entry optimization_hints
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.optimization-hints.json --input-hex 05 --expect-values 1,2 --expect-optimization-hint-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.objectsize-stack.json --entry objectsize_stack
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.objectsize-stack.json --expect-values 6 --expect-objectsize-intrinsic --expect-stack
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.objectsize-union.json --entry objectsize_union
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.objectsize-union.json --input-hex 00 --expect-values 1,2 --expect-objectsize-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.objectsize-null.json --entry objectsize_null
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.objectsize-null.json --expect-values 0 --expect-objectsize-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.objectsize-input.json --entry objectsize_dynamic_input
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.objectsize-input.json --input-hex 414243 --expect-values 2 --expect-objectsize-intrinsic --expect-dynamic-objectsize-intrinsic --expect-input-buffer
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.objectsize-heap.json --entry objectsize_dynamic_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.objectsize-heap.json --input-hex 0300000000000000 --expect-values 3 --expect-objectsize-intrinsic --expect-dynamic-objectsize-intrinsic --expect-nullable-heap
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.objectsize-static-runtime.json --entry objectsize_static_runtime
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.objectsize-static-runtime.json --input-hex 0300000000000000 --expect-values 18446744073709551615 --expect-objectsize-intrinsic --expect-nullable-heap
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.objectsize-realloc.json --entry objectsize_dynamic_realloc
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.objectsize-realloc.json --input-hex 0200000000000000 --expect-values 2 --expect-objectsize-intrinsic --expect-dynamic-objectsize-intrinsic
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.objectsize-realloc.json --input-hex 0500000000000000 --expect-values 0 --expect-objectsize-intrinsic --expect-dynamic-objectsize-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.objectsize-realloc-union.json --entry objectsize_dynamic_realloc_union
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.objectsize-realloc-union.json --input-hex 000200000000000000 --expect-values 1,2 --expect-objectsize-intrinsic --expect-dynamic-objectsize-intrinsic --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-global.json --entry pointer_memory_global
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-global.json --expect-values 77 --expect-pointer-memory --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-stack.json --entry pointer_memory_stack
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-stack.json --input-hex 00 --expect-values 1,2 --expect-pointer-memory --expect-pointer-union --expect-stack
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-symbolic.json --entry pointer_memory_symbolic_gep
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-symbolic.json --input-hex 00 --expect-values 1,2 --expect-pointer-memory --expect-pointer-union --expect-symbolic-pointer-memory
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=2 %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-heap.json --entry pointer_memory_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-heap.json --expect-values 66 --expect-pointer-memory --expect-pointer-union --expect-heap-pool
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-null.json --entry pointer_memory_null
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-null.json --expect-values 1 --expect-pointer-memory --expect-stack
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-conditional.json --entry pointer_memory_conditional
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-conditional.json --input-hex 00 --expect-values 1,2 --expect-pointer-memory --expect-pointer-memory-merge --expect-pointer-union --expect-stack
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-nested.json --entry pointer_memory_nested_merge
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-nested.json --input-hex 0000 --expect-values 1,1,2 --expect-pointer-memory --expect-pointer-memory-merge --expect-acyclic-pointer-memory-ssa --expect-pointer-union --expect-stack
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.function-pointer-memory-conditional.json --entry function_pointer_memory_conditional
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.function-pointer-memory-conditional.json --input-hex 0005 --expect-values 6,7 --expect-pointer-memory --expect-function-pointer-memory --expect-pointer-memory-merge --expect-indirect-call --expect-stack
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.function-pointer-memory-overwrite.json --entry function_pointer_memory_overwrite
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.function-pointer-memory-overwrite.json --input-hex 0005 --expect-values 6,7 --expect-pointer-memory --expect-function-pointer-memory --expect-indirect-call
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.function-pointer-memory-initial-merge.json --entry function_pointer_memory_initial_merge
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.function-pointer-memory-initial-merge.json --input-hex 0005 --expect-values 6,7 --expect-pointer-memory --expect-function-pointer-memory --expect-indirect-call --expect-pointer-initial-definition-merge
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-incomplete.json --entry bad_pointer_memory_incomplete
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-incomplete.json --expect-rejected "does not cover every predecessor"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-cycle.json --entry pointer_memory_cycle
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-cycle.json --expect-values 65 --expect-pointer-memory --expect-cyclic-pointer-memory-ssa --expect-stack
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-cycle-write.json --entry pointer_memory_cycle_write
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-cycle-write.json --expect-values 66 --expect-pointer-memory --expect-pointer-memory-merge --expect-cyclic-pointer-memory-ssa --expect-pointer-union --expect-stack
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-equivalent-gep.json --entry pointer_memory_equivalent_gep
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-equivalent-gep.json --expect-values 65 --expect-pointer-memory --expect-canonical-pointer-cell --expect-stack
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-distinct-gep.json --entry pointer_memory_distinct_gep
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-distinct-gep.json --expect-values 65 --expect-pointer-memory --expect-canonical-pointer-cell --expect-stack
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-uninitialized-cycle.json --entry bad_pointer_memory_uninitialized_cycle
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-uninitialized-cycle.json --expect-rejected "does not cover every predecessor"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-memory-overwrite.json --entry bad_pointer_memory_overwrite
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-memory-overwrite.json --expect-rejected "overwritten by a non-pointer value"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.function-pointer-memory-global.json --entry function_pointer_memory_global
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.function-pointer-memory-global.json --input-hex 05 --expect-values 6 --expect-pointer-memory --expect-function-pointer-memory --expect-indirect-call
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.function-pointer-memory-stack.json --entry function_pointer_memory_stack
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.function-pointer-memory-stack.json --input-hex 0005 --expect-values 6,7 --expect-pointer-memory --expect-function-pointer-memory --expect-indirect-call --expect-stack
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.data-pointer-table.json --entry data_pointer_table
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.data-pointer-table.json --input-hex 00 --expect-values 1,2 --expect-pointer-memory --expect-pointer-table --expect-pointer-union --expect-alias
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.function-pointer-table.json --entry function_pointer_table
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.function-pointer-table.json --input-hex 0005 --expect-values 6,7 --expect-pointer-memory --expect-function-pointer-memory --expect-pointer-table --expect-indirect-call --expect-alias
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.nounwind-invoke.json --entry nounwind_invoke
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.nounwind-invoke.json --input-hex 05 --expect-values 7 --expect-nounwind-invoke
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.nounwind-indirect-invoke.json --entry nounwind_indirect_invoke
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.nounwind-indirect-invoke.json --input-hex 0005 --expect-values 6,7 --expect-nounwind-invoke --expect-indirect-call
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.may-unwind-invoke.json --entry bad_may_unwind_invoke
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.may-unwind-invoke.json --expect-rejected "external calls require virtualization"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.cleanup-exception.json --entry handle_cleanup_exception
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.cleanup-exception.json --input-hex 00 --expect-values 7,99 --expect-cleanup-exception --expect-exception-ops --expect-unwind-call
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.cleanup-invoke-only.json --entry cleanup_invoke_only
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.cleanup-invoke-only.json --expect-values 5 --expect-cleanup-exception --expect-unwind-call
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.catch-all-exception.json --entry bad_catch_exception
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.catch-all-exception.json --input-hex 00 --expect-values 7,99 --expect-cleanup-exception --expect-exception-ops
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.typed-exception.json --entry handle_typed_exception
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.typed-exception.json --input-hex 00 --expect-values 7,111 --expect-cleanup-exception --expect-exception-ops --expect-unwind-call --expect-typed-exception
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.typed-exception-mismatch.json --entry miss_typed_exception
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.typed-exception-mismatch.json --input-hex 00 --expect-values 7 --expect-cleanup-exception --expect-exception-ops --expect-unwind-call --expect-unhandled-exception
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.exception-lifecycle.json --entry lifecycle_catch
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-lifecycle.json --input-hex 00 --expect-values 7,123 --expect-cleanup-exception --expect-exception-ops --expect-exception-lifecycle
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.exception-rethrow.json --entry lifecycle_rethrow
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-rethrow.json --input-hex 00 --expect-values 7,222 --expect-cleanup-exception --expect-exception-ops --expect-unwind-call --expect-exception-lifecycle
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.scalar-catch-object.json --entry scalar_catch_object
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.scalar-catch-object.json --input-hex 00 --expect-values 7,42 --expect-cleanup-exception --expect-exception-ops --expect-typed-exception --expect-exception-lifecycle --expect-scalar-catch-object
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-arena.json --entry exception_object_arena
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-arena.json --input-hex 00 --expect-values 7,42 --expect-cleanup-exception --expect-exception-ops --expect-typed-exception --expect-exception-lifecycle --expect-exception-object-arena
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-fields.json --entry exception_object_fields
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-fields.json --input-hex 00 --expect-values 7,47 --expect-cleanup-exception --expect-exception-ops --expect-typed-exception --expect-exception-lifecycle --expect-exception-object-arena --expect-exception-object-fields
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-cross-frame.json --entry exception_object_cross_frame
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-cross-frame.json --input-hex 00 --expect-values 7,84 --expect-cleanup-exception --expect-exception-ops --expect-unwind-call --expect-typed-exception --expect-exception-lifecycle --expect-exception-object-arena
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-destructor.json --entry bad_exception_object_destructor
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-destructor.json --expect-rejected "trivial null destructor"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-size.json --entry bad_exception_object_dynamic_size
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-size.json --expect-rejected "fixed non-zero size"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-interior.json --entry bad_exception_object_interior_throw
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-interior.json --expect-rejected "consumed by"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-mixed.json --entry bad_mixed_exception_producers
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-mixed.json --expect-rejected "mixes scalar exception summaries"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-use.json --entry bad_exception_object_materialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-use.json --expect-rejected "exception-object uses"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-gep.json --entry bad_exception_object_gep
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-gep.json --expect-rejected "exception-object uses"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-dynamic-field.json --entry bad_exception_object_dynamic_field
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-dynamic-field.json --expect-rejected "object materialization is unsupported"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-dynamic-store.json --entry bad_exception_object_dynamic_store
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-dynamic-store.json --expect-rejected "consumed by"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-oob-store.json --entry bad_exception_object_oob_store
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-oob-store.json --expect-rejected "consumed by"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-store.json --entry bad_exception_object_store
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-store.json --expect-rejected "exception-object uses"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-object-atomic.json --entry bad_exception_object_atomic
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-object-atomic.json --expect-rejected "exception-object uses"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.exception-filter.json --entry bad_exception_filter
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.exception-filter.json --expect-rejected "landingpad filters are unsupported"
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-null.json --entry pointer_select_null
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-null.json --input-hex 00 --expect-values 1 --expect-pointer-union
; RUN: not env SYMCC_LIVE_ALIAS_LIMIT=16 %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-wide.json --entry bad_pointer_union_wide
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-wide.json --expect-rejected "pointer union alias set exceeds"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.pointer-cycle.json --entry bad_pointer_phi_cycle
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer-cycle.json --expect-rejected "cyclic pointer PHI provenance"

; CLI: "continuation_ir_resume_supported":true
; CLI: "status":"lowered"
; BUFFER-CLI: "value":66
; BUFFER-CLI: "status":"lowered"

@word = global i32 287454020, align 4
@letters = constant [4 x i8] c"ABCD", align 4
@alias_bytes = global [4 x i8] zeroinitializer, align 4
@wide_alias_bytes = global [32 x i8] zeroinitializer, align 16
@alias_matrix = global [4 x [4 x i8]] zeroinitializer, align 16
@union_left = global i8 65, align 1
@union_right = global i8 66, align 1
@union_left_array = constant [2 x i8] c"AB", align 2
@union_right_array = constant [2 x i8] c"CD", align 2
@union_wide_left = constant [9 x i8] c"123456789", align 1
@union_wide_right = constant [9 x i8] c"abcdefghi", align 1
@compare_expected = constant [3 x i8] c"ABC", align 1
@compare_other = constant [3 x i8] c"ABD", align 1
@region_move_bytes = global [4 x i8] c"ABCD", align 1
@region_copy_source = constant [4 x i8] c"ABCD", align 1
@region_copy_destination = global [4 x i8] c"WXYZ", align 1
@region_symbolic_byte = global i8 0, align 1
@string_empty = constant [1 x i8] zeroinitializer, align 1
@string_ab = constant [3 x i8] c"AB\00", align 1
@string_ac = constant [3 x i8] c"AC\00", align 1
@string_abx = constant [3 x i8] c"ABX", align 1
@string_aby = constant [3 x i8] c"ABY", align 1
@search_bytes = constant [4 x i8] c"ABCA", align 1
@string_copy_destination = global [4 x i8] c"WXYZ", align 1
@string_copy_return_destination = global [4 x i8] c"WXYZ", align 1
@string_copy_union_destination = global [4 x i8] c"WXYZ", align 1
@string_n_copy_destination = global [4 x i8] c"WXYZ", align 1
@pointer_memory_target = global i8 77, align 1
@pointer_memory_slot = global ptr @pointer_memory_target, align 8
@function_pointer_memory_slot = global ptr @increment, align 8
@data_pointer_table_values = constant [2 x ptr]
    [ptr @union_left, ptr @union_right], align 8
@function_pointer_table_values = constant [2 x ptr]
    [ptr @increment, ptr @increment_two], align 8
@objectsize_left = global [4 x i8] zeroinitializer, align 1
@objectsize_right = global [6 x i8] zeroinitializer, align 1
@poison_memory_slot = global i8 0, align 1
@poison_memory_array = global [4 x i8] zeroinitializer, align 1
@poison_memory_matrix = global [2 x [2 x i8]] zeroinitializer, align 1
@symbolic_pointer_bytes = constant [2 x i8] c"AB", align 1
@symbolic_pointer_slot = global ptr @symbolic_pointer_bytes, align 8
@exception_type_a = external constant i8
@exception_type_b = external constant i8

declare ptr @malloc(i64)
declare ptr @calloc(i64, i64)
declare ptr @realloc(ptr, i64)
declare void @free(ptr)
declare i32 @memcmp(ptr, ptr, i64)
declare ptr @memcpy(ptr, ptr, i64)
declare ptr @memmove(ptr, ptr, i64)
declare ptr @memset(ptr, i32, i64)
declare i64 @strlen(ptr)
declare i32 @strcmp(ptr, ptr)
declare i32 @strncmp(ptr, ptr, i64)
declare ptr @memchr(ptr, i32, i64)
declare ptr @strchr(ptr, i32)
declare ptr @strcpy(ptr, ptr)
declare ptr @strncpy(ptr, ptr, i64)
declare i32 @__gxx_personality_v0(...)
declare void @__symcc_continuation_throw_if(i1, i64)
declare void @__symcc_continuation_throw_typed_if(i1, i64, ptr)
declare ptr @__cxa_begin_catch(ptr) nounwind
declare void @__cxa_end_catch()
declare void @__cxa_rethrow() noreturn
declare ptr @__cxa_allocate_exception(i64) nounwind
declare void @__cxa_throw(ptr, ptr, ptr) noreturn
declare void @exception_object_destructor(ptr)
declare i32 @llvm.eh.typeid.for(ptr)
declare void @may_unwind_external()
declare i32 @abs(i32)
declare i32 @modeled_add(i32, i32) nounwind willreturn readnone nofree nosync speculatable "symcc-continuation-model"="pure-v1:binary:add:0:1"
declare i32 @modeled_constant() nounwind willreturn readnone nofree nosync speculatable "symcc-continuation-model"="pure-v1:constant:42"
declare i32 @modeled_impure(i32, i32) nounwind willreturn nofree nosync speculatable "symcc-continuation-model"="pure-v1:binary:add:0:1"
declare i32 @modeled_malformed(i32) nounwind willreturn readnone nofree nosync speculatable "symcc-continuation-model"="pure-v1:identity:01"
declare i32 @modeled_bad_abi(i16) nounwind willreturn readnone nofree nosync speculatable "symcc-continuation-model"="pure-v1:identity:0"
declare i8 @"bad/name"(i8) nounwind willreturn readnone nofree nosync speculatable "symcc-continuation-model"="pure-v1:identity:0"
declare i32 @modeled_empty_part() nounwind willreturn readnone nofree nosync speculatable "symcc-continuation-model"="pure-v1::constant:1"
declare i32 @ntohl(i32)
declare i32 @llvm.bswap.i32(i32)
declare i8 @llvm.ctpop.i8(i8)
declare i8 @llvm.ctlz.i8(i8, i1 immarg)
declare i8 @llvm.cttz.i8(i8, i1 immarg)
declare i8 @llvm.bitreverse.i8(i8)
declare i8 @llvm.fshl.i8(i8, i8, i8)
declare i8 @llvm.fshr.i8(i8, i8, i8)
declare i8 @llvm.sadd.sat.i8(i8, i8)
declare i8 @llvm.uadd.sat.i8(i8, i8)
declare i8 @llvm.ssub.sat.i8(i8, i8)
declare i8 @llvm.usub.sat.i8(i8, i8)
declare i8 @llvm.sshl.sat.i8(i8, i8)
declare i8 @llvm.ushl.sat.i8(i8, i8)
declare {i8, i1} @llvm.sadd.with.overflow.i8(i8, i8)
declare {i8, i1} @llvm.uadd.with.overflow.i8(i8, i8)
declare {i8, i1} @llvm.ssub.with.overflow.i8(i8, i8)
declare {i8, i1} @llvm.usub.with.overflow.i8(i8, i8)
declare {i8, i1} @llvm.smul.with.overflow.i8(i8, i8)
declare {i8, i1} @llvm.umul.with.overflow.i8(i8, i8)
declare i8 @llvm.abs.i8(i8, i1 immarg)
declare i8 @llvm.smax.i8(i8, i8)
declare i8 @llvm.smin.i8(i8, i8)
declare i8 @llvm.umax.i8(i8, i8)
declare i8 @llvm.umin.i8(i8, i8)
declare i8 @llvm.expect.i8(i8, i8)
declare i8 @llvm.expect.with.probability.i8(i8, i8, double)
declare i64 @llvm.objectsize.i64.p0(ptr, i1, i1, i1)
declare i32 @labs(i32)

define i8 @increment(i8 %value) {
entry:
  %next = add i8 %value, 1
  ret i8 %next
}

define i8 @increment_two(i8 %value) {
entry:
  %next = add i8 %value, 2
  ret i8 %next
}

define i8 @nounwind_increment(i8 %value) nounwind {
entry:
  %result = add i8 %value, 1
  ret i8 %result
}

define i8 @nounwind_increment_two(i8 %value) nounwind {
entry:
  %result = add i8 %value, 2
  ret i8 %result
}

define i8 @may_unwind_helper(i8 %value) {
entry:
  call void @may_unwind_external()
  ret i8 %value
}

define i8 @nounwind_invoke(i8 %value)
    personality ptr @__gxx_personality_v0 {
entry:
  %called = invoke i8 @nounwind_increment(i8 %value)
      to label %normal unwind label %cleanup

normal:
  %merged = phi i8 [ %called, %entry ]
  %result = add i8 %merged, 1
  ret i8 %result

cleanup:
  %landing = landingpad { ptr, i32 } cleanup
  ret i8 99
}

define i8 @nounwind_indirect_invoke(i1 %choose, i8 %value)
    personality ptr @__gxx_personality_v0 {
entry:
  %target = select i1 %choose, ptr @nounwind_increment,
      ptr @nounwind_increment_two
  %called = invoke i8 %target(i8 %value)
      to label %normal unwind label %cleanup

normal:
  ret i8 %called

cleanup:
  %landing = landingpad { ptr, i32 } cleanup
  ret i8 99
}

define i8 @bad_may_unwind_invoke()
    personality ptr @__gxx_personality_v0 {
entry:
  %called = invoke i8 @may_unwind_helper(i8 5)
      to label %normal unwind label %cleanup

normal:
  ret i8 %called

cleanup:
  %landing = landingpad { ptr, i32 } cleanup
  ret i8 99
}

define i64 @cleanup_thrower(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  invoke void @__symcc_continuation_throw_if(i1 %throws, i64 42)
      to label %normal unwind label %cleanup

normal:
  ret i64 7

cleanup:
  %landing = landingpad { ptr, i32 } cleanup
  resume { ptr, i32 } %landing
}

define i64 @handle_cleanup_exception(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  %value = invoke i64 @cleanup_thrower(i1 %throws)
      to label %normal unwind label %caught

normal:
  ret i64 %value

caught:
  %landing = landingpad { ptr, i32 } cleanup
  ret i64 99
}

define i8 @cleanup_invoke_only()
    personality ptr @__gxx_personality_v0 {
entry:
  %value = invoke i8 @cleanup_invoke_target()
      to label %normal unwind label %caught

normal:
  ret i8 %value

caught:
  %landing = landingpad { ptr, i32 } cleanup
  ret i8 99
}

define i8 @cleanup_invoke_target() {
entry:
  ret i8 5
}

define i64 @bad_catch_exception(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  invoke void @__symcc_continuation_throw_if(i1 %throws, i64 42)
      to label %normal unwind label %caught

normal:
  ret i64 7

caught:
  %landing = landingpad { ptr, i32 }
      catch ptr null
  ret i64 99
}

define i64 @typed_exception_source(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  invoke void @__symcc_continuation_throw_typed_if(
      i1 %throws, i64 42, ptr @exception_type_a)
      to label %normal unwind label %cleanup

normal:
  ret i64 7

cleanup:
  %landing = landingpad { ptr, i32 } cleanup
  resume { ptr, i32 } %landing
}

define i64 @handle_typed_exception(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  %value = invoke i64 @typed_exception_source(i1 %throws)
      to label %normal unwind label %landing

normal:
  ret i64 %value

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %selector = extractvalue { ptr, i32 } %exception, 1
  %expected = call i32 @llvm.eh.typeid.for(ptr @exception_type_a)
  %matches = icmp eq i32 %selector, %expected
  br i1 %matches, label %caught, label %rethrow

caught:
  ret i64 111

rethrow:
  resume { ptr, i32 } %exception
}

define i64 @miss_typed_exception(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  %value = invoke i64 @typed_exception_source(i1 %throws)
      to label %normal unwind label %landing

normal:
  ret i64 %value

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_b
  resume { ptr, i32 } %exception
}

define i64 @lifecycle_catch(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  invoke void @__symcc_continuation_throw_typed_if(
      i1 %throws, i64 42, ptr @exception_type_a)
      to label %normal unwind label %landing

normal:
  ret i64 7

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  call void @__cxa_end_catch()
  ret i64 123
}

define i64 @lifecycle_rethrow_inner(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  invoke void @__symcc_continuation_throw_typed_if(
      i1 %throws, i64 42, ptr @exception_type_a)
      to label %normal unwind label %landing

normal:
  ret i64 7

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  invoke void @__cxa_rethrow()
      to label %unreachable unwind label %cleanup

cleanup:
  %resumed = landingpad { ptr, i32 } cleanup
  invoke void @__cxa_end_catch()
      to label %resume unwind label %terminate

resume:
  resume { ptr, i32 } %resumed

terminate:
  %termination = landingpad { ptr, i32 }
      catch ptr null
  unreachable

unreachable:
  unreachable
}

define i64 @lifecycle_rethrow(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  %value = invoke i64 @lifecycle_rethrow_inner(i1 %throws)
      to label %normal unwind label %landing

normal:
  ret i64 %value

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  call void @__cxa_end_catch()
  ret i64 222
}

define i64 @scalar_catch_object(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  %value = invoke i64 @typed_exception_source(i1 %throws)
      to label %normal unwind label %landing

normal:
  ret i64 %value

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %selector = extractvalue { ptr, i32 } %exception, 1
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  %payload = load i64, ptr %object, align 8
  call void @__cxa_end_catch()
  ret i64 %payload
}

define i64 @exception_object_arena(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  br i1 %throws, label %raise, label %normal

normal:
  ret i64 7

raise:
  %allocated = call ptr @__cxa_allocate_exception(i64 8)
  store i64 42, ptr %allocated, align 8
  invoke void @__cxa_throw(
      ptr %allocated, ptr @exception_type_a, ptr null)
      to label %unreachable unwind label %landing

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  %payload = load i64, ptr %object, align 8
  call void @__cxa_end_catch()
  ret i64 %payload

unreachable:
  unreachable
}

define i64 @exception_object_fields(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  br i1 %throws, label %raise, label %normal

normal:
  ret i64 7

raise:
  %allocated = call ptr @__cxa_allocate_exception(i64 12)
  %allocated.first = getelementptr { i32, [2 x i32] }, ptr %allocated, i64 0, i32 0
  %allocated.second = getelementptr { i32, [2 x i32] }, ptr %allocated, i64 0, i32 1, i64 0
  %allocated.third = getelementptr { i32, [2 x i32] }, ptr %allocated, i64 0, i32 1, i64 1
  store i32 5, ptr %allocated.first, align 4
  store i32 19, ptr %allocated.second, align 4
  store i32 23, ptr %allocated.third, align 4
  invoke void @__cxa_throw(
      ptr %allocated, ptr @exception_type_a, ptr null)
      to label %unreachable unwind label %landing

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  %first.field = getelementptr { i32, [2 x i32] }, ptr %object, i64 0, i32 0
  %second.field = getelementptr { i32, [2 x i32] }, ptr %object, i64 0, i32 1, i64 0
  %third.field = getelementptr { i32, [2 x i32] }, ptr %object, i64 0, i32 1, i64 1
  %first = load i32, ptr %first.field, align 4
  %second = load i32, ptr %second.field, align 4
  %third = load i32, ptr %third.field, align 4
  %partial = add i32 %first, %second
  %sum = add i32 %partial, %third
  %result = zext i32 %sum to i64
  call void @__cxa_end_catch()
  ret i64 %result

unreachable:
  unreachable
}

define i64 @exception_object_source() {
entry:
  %allocated = call ptr @__cxa_allocate_exception(i64 8)
  store i64 84, ptr %allocated, align 8
  call void @__cxa_throw(
      ptr %allocated, ptr @exception_type_a, ptr null)
  unreachable
}

define i64 @exception_object_cross_frame(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  br i1 %throws, label %raise, label %normal

normal:
  ret i64 7

raise:
  %ignored = invoke i64 @exception_object_source()
      to label %unreachable unwind label %landing

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  %payload = load i64, ptr %object, align 8
  call void @__cxa_end_catch()
  ret i64 %payload

unreachable:
  unreachable
}

define void @bad_exception_object_destructor() {
entry:
  %allocated = call ptr @__cxa_allocate_exception(i64 8)
  store i64 42, ptr %allocated, align 8
  call void @__cxa_throw(
      ptr %allocated, ptr @exception_type_a,
      ptr @exception_object_destructor)
  unreachable
}

define void @bad_exception_object_dynamic_size(i64 %size) {
entry:
  %allocated = call ptr @__cxa_allocate_exception(i64 %size)
  store i64 42, ptr %allocated, align 8
  call void @__cxa_throw(
      ptr %allocated, ptr @exception_type_a, ptr null)
  unreachable
}

define void @bad_exception_object_interior_throw() {
entry:
  %allocated = call ptr @__cxa_allocate_exception(i64 8)
  store i64 42, ptr %allocated, align 8
  %interior = getelementptr i8, ptr %allocated, i64 1
  call void @__cxa_throw(
      ptr %interior, ptr @exception_type_a, ptr null)
  unreachable
}

define void @bad_mixed_exception_producers(i1 %object_path)
    personality ptr @__gxx_personality_v0 {
entry:
  br i1 %object_path, label %object, label %scalar

scalar:
  invoke void @__symcc_continuation_throw_typed_if(
      i1 true, i64 11, ptr @exception_type_a)
      to label %unreachable unwind label %cleanup

object:
  %allocated = call ptr @__cxa_allocate_exception(i64 8)
  store i64 42, ptr %allocated, align 8
  invoke void @__cxa_throw(
      ptr %allocated, ptr @exception_type_a, ptr null)
      to label %unreachable unwind label %cleanup

cleanup:
  %exception = landingpad { ptr, i32 }
      cleanup
  resume { ptr, i32 } %exception

unreachable:
  unreachable
}

define i64 @bad_exception_object_materialization(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  invoke void @__symcc_continuation_throw_typed_if(
      i1 %throws, i64 42, ptr @exception_type_a)
      to label %normal unwind label %landing

normal:
  ret i64 7

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  %address = ptrtoint ptr %object to i64
  call void @__cxa_end_catch()
  ret i64 %address
}

define i8 @bad_exception_object_gep(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  invoke void @__symcc_continuation_throw_typed_if(
      i1 %throws, i64 42, ptr @exception_type_a)
      to label %normal unwind label %landing

normal:
  ret i8 7

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  %field = getelementptr i8, ptr %object, i64 1
  %payload = load i8, ptr %field, align 1
  call void @__cxa_end_catch()
  ret i8 %payload
}

define i8 @bad_exception_object_dynamic_field(i1 %throws, i64 %index)
    personality ptr @__gxx_personality_v0 {
entry:
  br i1 %throws, label %raise, label %normal

normal:
  ret i8 7

raise:
  %allocated = call ptr @__cxa_allocate_exception(i64 8)
  store i64 42, ptr %allocated, align 8
  invoke void @__cxa_throw(
      ptr %allocated, ptr @exception_type_a, ptr null)
      to label %unreachable unwind label %landing

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  %field = getelementptr i8, ptr %object, i64 %index
  %payload = load i8, ptr %field, align 1
  call void @__cxa_end_catch()
  ret i8 %payload

unreachable:
  unreachable
}

define void @bad_exception_object_dynamic_store(i64 %index) {
entry:
  %allocated = call ptr @__cxa_allocate_exception(i64 8)
  %field = getelementptr i8, ptr %allocated, i64 %index
  store i8 42, ptr %field, align 1
  call void @__cxa_throw(
      ptr %allocated, ptr @exception_type_a, ptr null)
  unreachable
}

define void @bad_exception_object_oob_store() {
entry:
  %allocated = call ptr @__cxa_allocate_exception(i64 8)
  %field = getelementptr i8, ptr %allocated, i64 8
  store i8 42, ptr %field, align 1
  call void @__cxa_throw(
      ptr %allocated, ptr @exception_type_a, ptr null)
  unreachable
}

define i64 @bad_exception_object_store(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  invoke void @__symcc_continuation_throw_typed_if(
      i1 %throws, i64 42, ptr @exception_type_a)
      to label %normal unwind label %landing

normal:
  ret i64 7

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  store i64 9, ptr %object, align 8
  call void @__cxa_end_catch()
  ret i64 9
}

define i64 @bad_exception_object_atomic(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  invoke void @__symcc_continuation_throw_typed_if(
      i1 %throws, i64 42, ptr @exception_type_a)
      to label %normal unwind label %landing

normal:
  ret i64 7

landing:
  %exception = landingpad { ptr, i32 }
      catch ptr @exception_type_a
  %token = extractvalue { ptr, i32 } %exception, 0
  %object = call ptr @__cxa_begin_catch(ptr %token)
  %payload = load atomic i64, ptr %object monotonic, align 8
  call void @__cxa_end_catch()
  ret i64 %payload
}

define i64 @bad_exception_filter(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  %value = invoke i64 @typed_exception_source(i1 %throws)
      to label %normal unwind label %landing

normal:
  ret i64 %value

landing:
  %exception = landingpad { ptr, i32 }
      filter [1 x ptr] [ptr @exception_type_a]
  resume { ptr, i32 } %exception
}

define i8 @check(i8 %input) {
entry:
  switch i8 %input, label %miss [
    i8 65, label %hit
    i8 66, label %second
  ]

hit:
  %next = call i8 @increment(i8 %input)
  br label %merge

second:
  %selected = select i1 true, i8 7, i8 8
  br label %merge

miss:
  br label %merge

merge:
  %result = phi i8 [ %next, %hit ], [ %selected, %second ], [ 0, %miss ]
  ret i8 %result
}

define i8 @bad(ptr %address) {
entry:
  %value = load i8, ptr %address
  ret i8 %value
}

define i8 @phi_swap() {
entry:
  br label %loop

loop:
  %a = phi i8 [ 1, %entry ], [ %b, %body ]
  %b = phi i8 [ 2, %entry ], [ %a, %body ]
  %first = phi i1 [ true, %entry ], [ false, %body ]
  br i1 %first, label %body, label %exit

body:
  br label %loop

exit:
  ret i8 %a
}

define i8 @recursive(i8 %value) {
entry:
  %next = call i8 @recursive(i8 %value)
  ret i8 %next
}

define i8 @pruned() {
entry:
  br i1 false, label %undefined, label %live

undefined:
  unreachable

live:
  ret i8 9
}

define i32 @memory_initial() {
entry:
  %value = load i32, ptr @word, align 4
  ret i32 %value
}

define i32 @memory_roundtrip(i32 %input) {
entry:
  %letter_address = getelementptr [4 x i8], ptr @letters, i64 0, i64 2
  %letter = load i8, ptr %letter_address, align 1
  store i32 %input, ptr @word, align 4
  %roundtrip = load i32, ptr @word, align 4
  %extended = zext i8 %letter to i32
  %result = add i32 %roundtrip, %extended
  ret i32 %result
}

define i8 @memory_branch(i32 %input) {
entry:
  store i32 %input, ptr @word, align 4
  %roundtrip = load i32, ptr @word, align 4
  %matched = icmp eq i32 %roundtrip, 305419896
  br i1 %matched, label %hit, label %miss

hit:
  ret i8 1

miss:
  ret i8 0
}

define i8 @bad_readonly() {
entry:
  %address = getelementptr [4 x i8], ptr @letters, i64 0, i64 1
  store i8 1, ptr %address, align 1
  ret i8 0
}

define i8 @symbolic_gep(i64 %index) {
entry:
  %address = getelementptr [4 x i8], ptr @letters, i64 0, i64 %index
  %value = load i8, ptr %address, align 1
  ret i8 %value
}

define i8 @buffer_check(ptr %input, i64 %size) {
entry:
  %short = icmp ult i64 %size, 3
  br i1 %short, label %too_short, label %read

too_short:
  ret i8 9

read:
  %first_address = getelementptr i8, ptr %input, i64 0
  %third_address = getelementptr i8, ptr %input, i64 2
  %first = load i8, ptr %first_address, align 1
  %third = load i8, ptr %third_address, align 1
  %first_match = icmp eq i8 %first, 65
  %third_match = icmp eq i8 %third, 67
  %match = and i1 %first_match, %third_match
  br i1 %match, label %hit, label %miss

hit:
  %second_address = getelementptr i8, ptr %input, i64 1
  %second = load i8, ptr %second_address, align 1
  ret i8 %second

miss:
  ret i8 0
}

define i8 @bad_buffer_read(ptr %input, i64 %size) {
entry:
  %outside = getelementptr i8, ptr %input, i64 3
  %value = load i8, ptr %outside, align 1
  ret i8 %value
}

define i8 @stack_callee(i8 %value) {
entry:
  %slots = alloca [2 x i8], align 2
  %first = getelementptr [2 x i8], ptr %slots, i64 0, i64 0
  store i8 %value, ptr %first, align 1
  %roundtrip = load i8, ptr %first, align 1
  ret i8 %roundtrip
}

define i8 @stack_calls(i8 %input) {
entry:
  %first = call i8 @stack_callee(i8 %input)
  %second = call i8 @stack_callee(i8 5)
  %sum = add i8 %first, %second
  ret i8 %sum
}

define i8 @stack_branch(i32 %input) {
entry:
  %slot = alloca [1 x i32], align 4
  %address = getelementptr [1 x i32], ptr %slot, i64 0, i64 0
  store i32 %input, ptr %address, align 4
  %roundtrip = load i32, ptr %address, align 4
  %matched = icmp eq i32 %roundtrip, 305419896
  br i1 %matched, label %hit, label %miss

hit:
  ret i8 1

miss:
  ret i8 0
}

define i8 @bad_stack_uninitialized() {
entry:
  %slot = alloca [1 x i8], align 1
  %address = getelementptr [1 x i8], ptr %slot, i64 0, i64 0
  %value = load i8, ptr %address, align 1
  ret i8 %value
}

define i8 @bad_dynamic_stack(i64 %size) {
entry:
  %slot = alloca i8, i64 %size, align 1
  %address = getelementptr i8, ptr %slot, i64 0
  store i8 1, ptr %address, align 1
  %value = load i8, ptr %address, align 1
  ret i8 %value
}

define i32 @heap_roundtrip(i32 %input) {
entry:
  %memory = call ptr @malloc(i64 8)
  %first = getelementptr i32, ptr %memory, i64 0
  %second = getelementptr i32, ptr %memory, i64 1
  store i32 %input, ptr %first, align 4
  store i32 7, ptr %second, align 4
  %left = load i32, ptr %first, align 4
  %right = load i32, ptr %second, align 4
  %result = add i32 %left, %right
  call void @free(ptr %memory)
  ret i32 %result
}

define i8 @heap_branch(i32 %input) {
entry:
  %memory = call ptr @malloc(i64 4)
  store i32 %input, ptr %memory, align 4
  %roundtrip = load i32, ptr %memory, align 4
  %matched = icmp eq i32 %roundtrip, 305419896
  call void @free(ptr %memory)
  br i1 %matched, label %hit, label %miss

hit:
  ret i8 1

miss:
  ret i8 0
}

define i8 @heap_multi_instance() {
entry:
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %loop ]
  %sum = phi i8 [ 0, %entry ], [ %updated, %loop ]
  %memory = call ptr @malloc(i64 1)
  store i8 %iteration, ptr %memory, align 1
  %value = load i8, ptr %memory, align 1
  %biased = add i8 %value, 10
  %updated = add i8 %sum, %biased
  %next = add i8 %iteration, 1
  %again = icmp ult i8 %next, 2
  br i1 %again, label %loop, label %exit

exit:
  ret i8 %updated
}

define i8 @bad_heap_pool_exhaustion() {
entry:
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %loop ]
  %memory = call ptr @malloc(i64 1)
  store i8 %iteration, ptr %memory, align 1
  %next = add i8 %iteration, 1
  %again = icmp ult i8 %next, 3
  br i1 %again, label %loop, label %exit

exit:
  ret i8 0
}

define i8 @bad_heap_uaf() {
entry:
  %memory = call ptr @malloc(i64 1)
  store i8 7, ptr %memory, align 1
  call void @free(ptr %memory)
  %value = load i8, ptr %memory, align 1
  ret i8 %value
}

define i8 @bad_heap_double_free() {
entry:
  %memory = call ptr @malloc(i64 1)
  call void @free(ptr %memory)
  call void @free(ptr %memory)
  ret i8 0
}

define i8 @bad_heap_uninitialized() {
entry:
  %memory = call ptr @malloc(i64 1)
  %value = load i8, ptr %memory, align 1
  call void @free(ptr %memory)
  ret i8 %value
}

define i8 @dynamic_heap(i64 %size) {
entry:
  %memory = call ptr @malloc(i64 %size)
  %failed = icmp eq ptr %memory, null
  br i1 %failed, label %oom, label %allocated

allocated:
  store i8 1, ptr %memory, align 1
  call void @free(ptr %memory)
  ret i8 0

oom:
  ret i8 1
}

define i8 @calloc_heap(i64 %count) {
entry:
  %memory = call ptr @calloc(i64 %count, i64 1)
  %failed = icmp eq ptr %memory, null
  br i1 %failed, label %oom, label %allocated

allocated:
  %value = load i8, ptr %memory, align 1
  call void @free(ptr %memory)
  ret i8 %value

oom:
  ret i8 1
}

define i8 @realloc_heap(i64 %size) {
entry:
  %memory = call ptr @malloc(i64 4)
  store i16 4660, ptr %memory, align 1
  %resized = call ptr @realloc(ptr %memory, i64 %size)
  %failed = icmp eq ptr %resized, null
  br i1 %failed, label %oom, label %allocated

allocated:
  %value = load i8, ptr %resized, align 1
  call void @free(ptr %resized)
  ret i8 %value

oom:
  ret i8 1
}

define i8 @heap_select_free(i1 %choose) {
entry:
  %left = call ptr @malloc(i64 1)
  %right = call ptr @malloc(i64 1)
  %victim = select i1 %choose, ptr %left, ptr %right
  %value = select i1 %choose, i8 22, i8 11
  call void @free(ptr %victim)
  ret i8 %value
}

define i8 @heap_phi_free(i1 %choose) {
entry:
  %left = call ptr @malloc(i64 1)
  %right = call ptr @malloc(i64 1)
  br i1 %choose, label %take_left, label %take_right

take_left:
  br label %merge

take_right:
  br label %merge

merge:
  %victim = phi ptr [ %left, %take_left ], [ %right, %take_right ]
  %value = phi i8 [ 42, %take_left ], [ 31, %take_right ]
  call void @free(ptr %victim)
  ret i8 %value
}

define i8 @heap_null_free(i1 %choose) {
entry:
  %memory = call ptr @malloc(i64 1)
  %victim = select i1 %choose, ptr %memory, ptr null
  call void @free(ptr %victim)
  ret i8 7
}

define i8 @bad_heap_mixed_free(i1 %choose) {
entry:
  %memory = call ptr @malloc(i64 1)
  %victim = select i1 %choose, ptr %memory, ptr @union_left
  call void @free(ptr %victim)
  ret i8 0
}

define i8 @bad_heap_interior_free() {
entry:
  %memory = call ptr @malloc(i64 2)
  %interior = getelementptr i8, ptr %memory, i64 1
  call void @free(ptr %interior)
  ret i8 0
}

define i8 @symbolic_store(i8 %index) {
entry:
  %address = getelementptr [4 x i8], ptr @alias_bytes, i64 0, i8 %index
  store i8 7, ptr %address, align 1
  %observed_address = getelementptr [4 x i8], ptr @alias_bytes, i64 0, i64 2
  %observed = load i8, ptr %observed_address, align 1
  %matched = icmp eq i8 %observed, 7
  br i1 %matched, label %hit, label %miss

hit:
  ret i8 1

miss:
  ret i8 0
}

define i8 @heap_symbolic_index(i8 %index) {
entry:
  %memory = call ptr @malloc(i64 4)
  %address = getelementptr i8, ptr %memory, i8 %index
  store i8 7, ptr %address, align 1
  %value = load i8, ptr %address, align 1
  call void @free(ptr %memory)
  ret i8 %value
}

define i8 @bad_wide_alias(i8 %index) {
entry:
  %address = getelementptr [32 x i8], ptr @wide_alias_bytes, i64 0, i8 %index
  %value = load i8, ptr %address, align 1
  ret i8 %value
}

define i8 @stack_symbolic_index(i8 %index) {
entry:
  %memory = alloca [4 x i8], align 4
  %address = getelementptr [4 x i8], ptr %memory, i64 0, i8 %index
  store i8 7, ptr %address, align 1
  %value = load i8, ptr %address, align 1
  ret i8 %value
}

define i8 @buffer_symbolic_index(ptr %input, i64 %size) {
entry:
  %index = load i8, ptr %input, align 1
  %extended = zext i8 %index to i64
  %address = getelementptr i8, ptr %input, i64 %extended
  %value = load i8, ptr %address, align 1
  ret i8 %value
}

define i8 @bad_multiterm_alias(i8 %row, i8 %column) {
entry:
  %address = getelementptr [4 x [4 x i8]], ptr @alias_matrix, i64 0, i8 %row, i8 %column
  %value = load i8, ptr %address, align 1
  ret i8 %value
}

define i8 @nested_symbolic_gep(i8 %index) {
entry:
  %element = getelementptr [4 x i8], ptr @letters, i64 0, i8 %index
  %next = getelementptr i8, ptr %element, i64 1
  %value = load i8, ptr %next, align 1
  ret i8 %value
}

define i8 @nested_inbounds_gep(i8 %index) {
entry:
  %element = getelementptr inbounds [4 x i8], ptr @letters, i64 0, i8 %index
  %back = getelementptr i8, ptr %element, i64 -2
  %value = load i8, ptr %back, align 1
  ret i8 %value
}

define i8 @nested_inbounds_base(i8 %index) {
entry:
  %element = getelementptr [4 x i8], ptr @letters, i64 0, i8 %index
  %back = getelementptr inbounds i8, ptr %element, i64 -2
  %value = load i8, ptr %back, align 1
  ret i8 %value
}

define i8 @pointer_select(i1 %choose) {
entry:
  %pointer = select i1 %choose, ptr @union_left, ptr @union_right
  %value = load i8, ptr %pointer, align 1
  %left = icmp eq i8 %value, 65
  br i1 %left, label %is_left, label %is_right

is_left:
  ret i8 1

is_right:
  ret i8 2
}

define i8 @pointer_phi(i1 %choose) {
entry:
  br i1 %choose, label %left, label %right

left:
  br label %merge

right:
  br label %merge

merge:
  %pointer = phi ptr [ @union_left, %left ], [ @union_right, %right ]
  %value = load i8, ptr %pointer, align 1
  %is_left = icmp eq i8 %value, 65
  br i1 %is_left, label %left_result, label %right_result

left_result:
  ret i8 1

right_result:
  ret i8 2
}

define i8 @pointer_select_store(i1 %choose) {
entry:
  %pointer = select i1 %choose, ptr @union_left, ptr @union_right
  store i8 7, ptr %pointer, align 1
  %left = load i8, ptr @union_left, align 1
  %changed = icmp eq i8 %left, 7
  br i1 %changed, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 0
}

define i8 @pointer_select_dynamic(i1 %choose, i8 %index) {
entry:
  %left_pointer = getelementptr [2 x i8], ptr @union_left_array, i64 0, i8 %index
  %right_pointer = getelementptr [2 x i8], ptr @union_right_array, i64 0, i8 %index
  %pointer = select i1 %choose, ptr %left_pointer, ptr %right_pointer
  %value = load i8, ptr %pointer, align 1
  %matched = icmp eq i8 %value, 66
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 0
}

define i8 @pointer_select_gep(i1 %choose) {
entry:
  %base = select i1 %choose, ptr @union_left_array, ptr @union_right_array
  %pointer = getelementptr [2 x i8], ptr %base, i64 0, i64 1
  %value = load i8, ptr %pointer, align 1
  %matched = icmp eq i8 %value, 66
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 0
}

define i8 @cross_pointer_reader(ptr %pointer) {
entry:
  %second = getelementptr i8, ptr %pointer, i64 1
  %value = load i8, ptr %second, align 1
  ret i8 %value
}

define i8 @cross_pointer_arg(i1 %choose) {
entry:
  %base = select i1 %choose, ptr @union_left_array, ptr @union_right_array
  %value = call i8 @cross_pointer_reader(ptr %base)
  %matched = icmp eq i8 %value, 66
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 0
}

define ptr @echo_pointer(ptr %pointer) {
entry:
  ret ptr %pointer
}

define i8 @cross_pointer_echo(i1 %choose) {
entry:
  %base = select i1 %choose, ptr @union_left_array, ptr @union_right_array
  %echoed = call ptr @echo_pointer(ptr %base)
  %value = call i8 @cross_pointer_reader(ptr %echoed)
  %matched = icmp eq i8 %value, 66
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 0
}

define i8 @cross_pointer_store_load(ptr %pointer, i8 %value) {
entry:
  store i8 %value, ptr %pointer, align 1
  %roundtrip = load i8, ptr %pointer, align 1
  ret i8 %roundtrip
}

define i8 @cross_stack_pointer(i8 %input) {
entry:
  %slot = alloca i8, align 1
  %value = call i8 @cross_pointer_store_load(ptr %slot, i8 %input)
  ret i8 %value
}

define ptr @make_heap_pointer() {
entry:
  %pointer = call ptr @malloc(i64 1)
  store i8 7, ptr %pointer, align 1
  ret ptr %pointer
}

define i8 @cross_pointer_return() {
entry:
  %pointer = call ptr @make_heap_pointer()
  %value = load i8, ptr %pointer, align 1
  call void @free(ptr %pointer)
  ret i8 %value
}

define ptr @bad_stack_pointer_return() {
entry:
  %slot = alloca i8, align 1
  ret ptr %slot
}

define i8 @bad_cross_stack_escape() {
entry:
  %pointer = call ptr @bad_stack_pointer_return()
  %value = load i8, ptr %pointer, align 1
  ret i8 %value
}

define i8 @cross_symbolic_pointer(i8 %index) {
entry:
  %pointer = getelementptr inbounds [2 x i8], ptr @union_left_array, i64 0, i8 %index
  %value = call i8 @cross_pointer_reader(ptr %pointer)
  ret i8 %value
}

define i8 @indirect_left(i8 %value) {
entry:
  %result = add i8 %value, 1
  ret i8 %result
}

define i8 @indirect_right(i8 %value) {
entry:
  %result = add i8 %value, 2
  ret i8 %result
}

define i8 @indirect_select(i1 %choose, i8 %value) {
entry:
  %target = select i1 %choose, ptr @indirect_left, ptr @indirect_right
  %result = call i8 %target(i8 %value)
  ret i8 %result
}

define i8 @indirect_phi(i1 %choose, i8 %value) {
entry:
  br i1 %choose, label %left, label %right

left:
  br label %merge

right:
  br label %merge

merge:
  %target = phi ptr [ @indirect_left, %left ], [ @indirect_right, %right ]
  %result = call i8 %target(i8 %value)
  ret i8 %result
}

define ptr @indirect_pointer_left(ptr %value) {
entry:
  ret ptr @union_left
}

define ptr @indirect_pointer_right(ptr %value) {
entry:
  ret ptr %value
}

define i8 @indirect_pointer_return(i1 %choose) {
entry:
  %target = select i1 %choose, ptr @indirect_pointer_left, ptr @indirect_pointer_right
  %argument = select i1 %choose, ptr @union_left, ptr @union_right
  %result = call ptr %target(ptr %argument)
  %value = load i8, ptr %result, align 1
  ret i8 %value
}

define i8 @memory_compare_entry(ptr %input, i64 %size) {
entry:
  %enough = icmp uge i64 %size, 3
  br i1 %enough, label %compare, label %short

compare:
  %expected = getelementptr [3 x i8], ptr @compare_expected, i64 0, i64 0
  %comparison = call i32 @memcmp(ptr %input, ptr %expected, i64 3)
  %equal = icmp eq i32 %comparison, 0
  br i1 %equal, label %match, label %mismatch

match:
  ret i8 1

mismatch:
  ret i8 0

short:
  ret i8 9
}

define i8 @memory_compare_order(ptr %input, i64 %size) {
entry:
  %expected = getelementptr [3 x i8], ptr @compare_expected, i64 0, i64 0
  %comparison = call i32 @memcmp(ptr %input, ptr %expected, i64 3)
  %less = icmp slt i32 %comparison, 0
  br i1 %less, label %before, label %not_before

before:
  ret i8 1

not_before:
  %greater = icmp sgt i32 %comparison, 0
  br i1 %greater, label %after, label %equal

after:
  ret i8 2

equal:
  ret i8 0
}

define i32 @memory_compare_zero() {
entry:
  %comparison = call i32 @memcmp(ptr null, ptr null, i64 0)
  ret i32 %comparison
}

define i8 @memory_compare_union(i1 %choose) {
entry:
  %left = select i1 %choose, ptr @compare_expected, ptr @compare_other
  %comparison = call i32 @memcmp(
      ptr %left, ptr @compare_expected, i64 3)
  %equal = icmp eq i32 %comparison, 0
  br i1 %equal, label %match, label %mismatch

match:
  ret i8 1

mismatch:
  ret i8 0
}

define i32 @bad_memory_compare_wide() {
entry:
  %comparison = call i32 @memcmp(
      ptr @compare_expected, ptr @compare_expected, i64 65)
  ret i32 %comparison
}

define i32 @region_memmove() {
entry:
  %destination = getelementptr [4 x i8], ptr @region_move_bytes, i64 0, i64 1
  %source = getelementptr [4 x i8], ptr @region_move_bytes, i64 0, i64 0
  call ptr @memmove(ptr %destination, ptr %source, i64 3)
  %result = load i32, ptr @region_move_bytes, align 1
  ret i32 %result
}

define i16 @region_memset(ptr %input, i64 %size) {
entry:
  call ptr @memset(ptr %input, i32 90, i64 2)
  %result = load i16, ptr %input, align 1
  ret i16 %result
}

define i8 @region_symbolic_set(i8 %value) {
entry:
  %wide = zext i8 %value to i32
  call ptr @memset(ptr @region_symbolic_byte, i32 %wide, i64 1)
  %result = load i8, ptr @region_symbolic_byte, align 1
  %matched = icmp eq i8 %result, 90
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 0
}

define i32 @region_memcpy() {
entry:
  call ptr @memcpy(
      ptr @region_copy_destination, ptr @region_copy_source, i64 4)
  %result = load i32, ptr @region_copy_destination, align 1
  ret i32 %result
}

define i8 @region_zero() {
entry:
  %result = call ptr @memset(ptr null, i32 1, i64 0)
  %is_null = icmp eq ptr %result, null
  %value = zext i1 %is_null to i8
  ret i8 %value
}

define ptr @bad_region_memcpy_overlap() {
entry:
  %destination = getelementptr [4 x i8], ptr @region_move_bytes, i64 0, i64 1
  %source = getelementptr [4 x i8], ptr @region_move_bytes, i64 0, i64 0
  %result = call ptr @memcpy(
      ptr %destination, ptr %source, i64 3)
  ret ptr %result
}

define i8 @string_length(i1 %choose) {
entry:
  %string = select i1 %choose, ptr @string_empty, ptr @string_ab
  %length = call i64 @strlen(ptr %string)
  %long = icmp eq i64 %length, 2
  br i1 %long, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 0
}

define i8 @string_input(ptr %input, i64 %size) {
entry:
  %length = call i64 @strlen(ptr %input)
  %uses_backing_zero = icmp eq i64 %length, %size
  br i1 %uses_backing_zero, label %bad, label %valid

bad:
  ret i8 1

valid:
  ret i8 0
}

define i8 @string_compare(i1 %choose) {
entry:
  %candidate = select i1 %choose, ptr @string_ab, ptr @string_ac
  %comparison = call i32 @strcmp(ptr %candidate, ptr @string_ab)
  %equal = icmp eq i32 %comparison, 0
  br i1 %equal, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 0
}

define i32 @string_n_compare() {
entry:
  %comparison = call i32 @strncmp(
      ptr @string_abx, ptr @string_aby, i64 2)
  ret i32 %comparison
}

define i32 @string_n_zero() {
entry:
  %comparison = call i32 @strncmp(ptr null, ptr null, i64 0)
  ret i32 %comparison
}

define i32 @bad_string_n_symbolic(i64 %length) {
entry:
  %comparison = call i32 @strncmp(
      ptr @string_ab, ptr @string_ac, i64 %length)
  ret i32 %comparison
}

define i8 @memory_search() {
entry:
  %found = call ptr @memchr(ptr @search_bytes, i32 67, i64 4)
  %value = load i8, ptr %found, align 1
  ret i8 %value
}

define i8 @memory_search_symbolic(i8 %needle) {
entry:
  %wide = zext i8 %needle to i32
  %found = call ptr @memchr(ptr @search_bytes, i32 %wide, i64 4)
  %missing = icmp eq ptr %found, null
  br i1 %missing, label %no, label %yes

yes:
  ret i8 1

no:
  ret i8 0
}

define i8 @memory_search_zero() {
entry:
  %found = call ptr @memchr(ptr null, i32 0, i64 0)
  %missing = icmp eq ptr %found, null
  %result = zext i1 %missing to i8
  ret i8 %result
}

define ptr @bad_memory_search_wide() {
entry:
  %found = call ptr @memchr(ptr @search_bytes, i32 65, i64 5)
  ret ptr %found
}

define i8 @string_search() {
entry:
  %found = call ptr @strchr(ptr @string_ab, i32 66)
  %value = load i8, ptr %found, align 1
  ret i8 %value
}

define i8 @string_search_union(i1 %choose) {
entry:
  %source = select i1 %choose, ptr @string_empty, ptr @string_ab
  %found = call ptr @strchr(ptr %source, i32 66)
  %missing = icmp eq ptr %found, null
  br i1 %missing, label %no, label %yes

yes:
  ret i8 1

no:
  ret i8 0
}

define i32 @string_copy() {
entry:
  %result = call ptr @strcpy(
      ptr @string_copy_destination, ptr @string_ab)
  %value = load i32, ptr @string_copy_destination, align 1
  ret i32 %value
}

define i8 @string_copy_return() {
entry:
  %result = call ptr @strcpy(
      ptr @string_copy_return_destination, ptr @string_ab)
  %same = icmp eq ptr %result, @string_copy_return_destination
  %value = zext i1 %same to i8
  ret i8 %value
}

define i8 @string_copy_union(i1 %choose) {
entry:
  %source = select i1 %choose, ptr @string_empty, ptr @string_ab
  %result = call ptr @strcpy(
      ptr @string_copy_union_destination, ptr %source)
  %slot = getelementptr [4 x i8],
      ptr @string_copy_union_destination, i64 0, i64 1
  %value = load i8, ptr %slot, align 1
  %copied = icmp eq i8 %value, 66
  br i1 %copied, label %copied_value, label %preserved_value

copied_value:
  ret i8 66

preserved_value:
  ret i8 88
}

define i32 @string_n_copy() {
entry:
  %result = call ptr @strncpy(
      ptr @string_n_copy_destination, ptr @string_ab, i64 4)
  %value = load i32, ptr @string_n_copy_destination, align 1
  ret i32 %value
}

define i8 @string_n_copy_zero() {
entry:
  %result = call ptr @strncpy(ptr null, ptr null, i64 0)
  %same = icmp eq ptr %result, null
  %value = zext i1 %same to i8
  ret i8 %value
}

define ptr @bad_string_copy_overlap() {
entry:
  %result = call ptr @strcpy(ptr @string_copy_destination,
                             ptr @string_copy_destination)
  ret ptr %result
}

define ptr @bad_string_n_copy_symbolic(i64 %length) {
entry:
  %result = call ptr @strncpy(
      ptr @string_n_copy_destination, ptr @string_ab, i64 %length)
  ret ptr %result
}

define i8 @defined_div(i8 %divisor) {
entry:
  %quotient = udiv i8 12, %divisor
  %matched = icmp eq i8 %quotient, 4
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 0
}

define i8 @defined_symbolic_shift(i8 %amount) {
entry:
  %shifted = shl i8 1, %amount
  %matched = icmp eq i8 %shifted, 128
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 0
}

define i8 @good_integer_flags() {
entry:
  %sum = add nuw nsw i8 5, 3
  %product = mul nuw nsw i8 %sum, 2
  %quotient = udiv exact i8 %product, 2
  %shifted = lshr exact i8 %quotient, 1
  %result = shl nuw nsw i8 %shifted, 2
  ret i8 %result
}

define i8 @bad_div_zero() {
entry:
  %result = udiv i8 12, 0
  ret i8 %result
}

define i8 @bad_sdiv_overflow() {
entry:
  %result = sdiv i8 -128, -1
  ret i8 %result
}

define i8 @bad_nuw_add() {
entry:
  %result = add nuw i8 255, 1
  ret i8 %result
}

define i8 @bad_nsw_mul() {
entry:
  %result = mul nsw i8 100, 2
  ret i8 %result
}

define i8 @bad_exact_div() {
entry:
  %result = udiv exact i8 7, 2
  ret i8 %result
}

define i8 @bad_exact_shift() {
entry:
  %result = lshr exact i8 3, 1
  ret i8 %result
}

define i8 @freeze_poison() {
entry:
  %value = freeze i8 poison
  %matched = icmp eq i8 %value, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_undef() {
entry:
  %value = freeze i8 undef
  %matched = icmp eq i8 %value, 7
  br i1 %matched, label %yes, label %no

yes:
  ret i8 3

no:
  ret i8 4
}

define i8 @scalar_abs(i32 %value) {
entry:
  %magnitude = call i32 @abs(i32 %value)
  %matched = icmp eq i32 %magnitude, 5
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i32 @scalar_byte_order() {
entry:
  %result = call i32 @ntohl(i32 16909060)
  ret i32 %result
}

define i32 @scalar_bswap() {
entry:
  %result = call i32 @llvm.bswap.i32(i32 16909060)
  ret i32 %result
}

define i32 @bad_scalar_signature() {
entry:
  %result = call i32 @labs(i32 5)
  ret i32 %result
}

define i8 @declarative_pure_add(i32 %value) {
entry:
  %result = call i32 @modeled_add(i32 %value, i32 5)
  %matched = icmp eq i32 %result, 8
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i32 @declarative_pure_constant() {
entry:
  %result = call i32 @modeled_constant()
  ret i32 %result
}

define i32 @declarative_pure_invoke() personality ptr @__gxx_personality_v0 {
entry:
  %result = invoke i32 @modeled_add(i32 2, i32 3)
      to label %normal unwind label %cleanup

normal:
  ret i32 %result

cleanup:
  %landing = landingpad { ptr, i32 }
      cleanup
  resume { ptr, i32 } %landing
}

define i32 @bad_declarative_pure_contract() {
entry:
  %result = call i32 @modeled_impure(i32 2, i32 3)
  ret i32 %result
}

define i32 @bad_declarative_pure_model() {
entry:
  %result = call i32 @modeled_malformed(i32 2)
  ret i32 %result
}

define i32 @bad_declarative_pure_abi() {
entry:
  %result = call i32 @modeled_bad_abi(i16 2)
  ret i32 %result
}

define i8 @bad_declarative_pure_name() {
entry:
  %result = call i8 @"bad/name"(i8 2)
  ret i8 %result
}

define i32 @bad_declarative_pure_empty_part() {
entry:
  %result = call i32 @modeled_empty_part()
  ret i32 %result
}

define i32 @bad_declarative_pure_zero_site() {
entry:
  %result = call i32 @modeled_constant(), !symcc.site_id !0
  ret i32 %result
}

define i32 @bad_declarative_pure_duplicate_site() {
entry:
  %first = call i32 @modeled_constant(), !symcc.site_id !1
  %second = call i32 @modeled_constant(), !symcc.site_id !1
  %result = add i32 %first, %second
  ret i32 %result
}

define i8 @bitcount_population(i8 %value) {
entry:
  %count = call i8 @llvm.ctpop.i8(i8 %value)
  %matched = icmp eq i8 %count, 2
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

!0 = !{i64 0}
!1 = !{i64 7}

define i8 @bitcount_leading_zero() {
entry:
  %count = call i8 @llvm.ctlz.i8(i8 0, i1 false)
  ret i8 %count
}

define i8 @bitcount_trailing(i8 %value) {
entry:
  %count = call i8 @llvm.cttz.i8(i8 %value, i1 false)
  %matched = icmp eq i8 %count, 3
  br i1 %matched, label %yes, label %no

yes:
  ret i8 3

no:
  ret i8 4
}

define i8 @bad_bitcount_zero_poison() {
entry:
  %count = call i8 @llvm.ctlz.i8(i8 0, i1 true)
  ret i8 %count
}

define i8 @freeze_wrapped_add() {
entry:
  %overflow = add nsw i8 127, 1
  %stable = freeze i8 %overflow
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_defined_add() {
entry:
  %sum = add nuw nsw i8 1, 1
  %stable = freeze i8 %sum
  ret i8 %stable
}

define i8 @bad_deferred_poison_multiple_use() {
entry:
  %overflow = add nsw i8 127, 1
  %consumed = icmp eq i8 %overflow, 0
  %stable = freeze i8 %overflow
  %result = select i1 %consumed, i8 %stable, i8 0
  ret i8 %result
}

define i8 @freeze_deferred_poison_multiple_sinks() {
entry:
  %overflow = add nsw i8 127, 1
  %first = freeze i8 %overflow
  %second = freeze i8 %overflow
  %first_match = icmp eq i8 %first, 42
  %second_match = icmp eq i8 %second, 43
  %both_match = and i1 %first_match, %second_match
  br i1 %both_match, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_transitive_cast() {
entry:
  %overflow = add nsw i8 127, 1
  %wide = sext i8 %overflow to i16
  %narrow = trunc i16 %wide to i8
  %stable = freeze i8 %narrow
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_transitive_compare() {
entry:
  %overflow = add nuw i8 -1, 1
  %mixed = xor i8 %overflow, 85
  %comparison = icmp eq i8 %mixed, 85
  %stable = freeze i1 %comparison
  br i1 %stable, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i16 @freeze_transitive_defined() {
entry:
  %sum = add nsw i8 1, 1
  %wide = zext i8 %sum to i16
  %biased = add i16 %wide, 1
  %stable = freeze i16 %biased
  ret i16 %stable
}

define i8 @freeze_deferred_division_zero() {
entry:
  %poisoned = udiv i8 7, 0
  %stable = freeze i8 %poisoned
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_deferred_sdiv_overflow() {
entry:
  %poisoned = sdiv i8 -128, -1
  %stable = freeze i8 %poisoned
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_deferred_exact_division() {
entry:
  %poisoned = udiv exact i8 7, 2
  %stable = freeze i8 %poisoned
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_deferred_defined_division() {
entry:
  %quotient = udiv exact i8 8, 2
  %stable = freeze i8 %quotient
  ret i8 %stable
}

define i8 @freeze_select_unselected_poison() {
entry:
  %poisoned = add nsw i8 127, 1
  %selected = select i1 false, i8 %poisoned, i8 7
  %stable = freeze i8 %selected
  ret i8 %stable
}

define i8 @freeze_select_selected_poison() {
entry:
  %poisoned = add nsw i8 127, 1
  %selected = select i1 true, i8 %poisoned, i8 7
  %stable = freeze i8 %selected
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_select_poison_condition() {
entry:
  %poisoned = add nuw i8 -1, 1
  %condition = icmp eq i8 %poisoned, 0
  %selected = select i1 %condition, i8 5, i8 6
  %stable = freeze i8 %selected
  %matched = icmp eq i8 %stable, 5
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_phi_unselected_poison() {
entry:
  br i1 false, label %poison, label %defined

poison:
  %poisoned = add nsw i8 127, 1
  br label %merge

defined:
  br label %merge

merge:
  %value = phi i8 [ %poisoned, %poison ], [ 7, %defined ]
  %stable = freeze i8 %value
  ret i8 %stable
}

define i8 @freeze_phi_selected_poison() {
entry:
  br i1 true, label %poison, label %defined

poison:
  %poisoned = add nsw i8 127, 1
  br label %merge

defined:
  br label %merge

merge:
  %value = phi i8 [ %poisoned, %poison ], [ 7, %defined ]
  %stable = freeze i8 %value
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_poison() {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  %loaded = load i8, ptr @poison_memory_slot, align 1
  store i8 0, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_defined() {
entry:
  %sum = add nsw i8 1, 1
  store i8 %sum, ptr @poison_memory_slot, align 1
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  ret i8 %stable
}

define i8 @freeze_memory_byte_lane_composition() {
entry:
  %first = add nsw i8 127, 1
  %first_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i8 %first, ptr %first_pointer, align 1
  %second = add nsw i8 1, 1
  %second_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 1
  store i8 %second, ptr %second_pointer, align 1
  br label %consume

consume:
  %load_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %loaded = load i16, ptr %load_pointer, align 1
  %stable = freeze i16 %loaded
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_byte_lane_initial_composition() {
entry:
  %poisoned = add nsw i8 127, 1
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i8 %poisoned, ptr %pointer, align 1
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i16 @freeze_memory_byte_lane_defined_composition() {
entry:
  %first = add nsw i8 1, 1
  %first_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i8 %first, ptr %first_pointer, align 1
  %second = add nsw i8 3, 1
  %second_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 1
  store i8 %second, ptr %second_pointer, align 1
  %loaded = load i16, ptr %first_pointer, align 1
  %stable = freeze i16 %loaded
  ret i16 %stable
}

define i8 @freeze_memory_byte_lane_overwrite_composition() {
entry:
  %wide_poison = add nsw i16 32767, 1
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i16 %wide_poison, ptr %pointer, align 1
  %defined = add nsw i8 1, 1
  %high_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 1
  store i8 %defined, ptr %high_pointer, align 1
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_byte_lane_phi(i1 %choose) {
entry:
  br i1 %choose, label %left, label %right

left:
  %left_poison = add nsw i8 127, 1
  %left_low = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i8 %left_poison, ptr %left_low, align 1
  %left_defined = add nsw i8 1, 1
  %left_high = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 1
  store i8 %left_defined, ptr %left_high, align 1
  br label %merge

right:
  %right_low_value = add nsw i8 1, 1
  %right_low = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i8 %right_low_value, ptr %right_low, align 1
  %right_high_value = add nsw i8 3, 1
  %right_high = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 1
  store i8 %right_high_value, ptr %right_high, align 1
  br label %merge

merge:
  %load_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %loaded = load i16, ptr %load_pointer, align 1
  %stable = freeze i16 %loaded
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_byte_lane_writer_graph_cross_function(i1 %choose) {
entry:
  %graph = call i8 @freeze_memory_byte_lane_overwrite_composition()
  %phi = call i8 @freeze_memory_byte_lane_phi(i1 %choose)
  %sum = add i8 %graph, %phi
  ret i8 %sum
}

define i8 @bad_freeze_memory_byte_lane_upstream_merge(
    i1 %outer, i1 %inner) {
entry:
  br i1 %outer, label %left_entry, label %right_entry

left_entry:
  br label %shared

right_entry:
  br label %shared

shared:
  br i1 %inner, label %low_path, label %high_path

low_path:
  %low_poison = add nsw i8 127, 1
  %low_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i8 %low_poison, ptr %low_pointer, align 1
  br label %merge

high_path:
  %high_poison = add nsw i8 127, 1
  %high_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 1
  store i8 %high_poison, ptr %high_pointer, align 1
  br label %merge

merge:
  %load_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %loaded = load i16, ptr %load_pointer, align 1
  %stable = freeze i16 %loaded
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_byte_lane_initial_phi(
    i1 %choose) {
entry:
  br i1 %choose, label %left, label %right

left:
  %left_poison = add nsw i8 127, 1
  %left_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i8 %left_poison, ptr %left_pointer, align 1
  br label %merge

right:
  %right_poison = add nsw i8 127, 1
  %right_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 1
  store i8 %right_poison, ptr %right_pointer, align 1
  br label %merge

merge:
  %load_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %loaded = load i16, ptr %load_pointer, align 1
  %stable = freeze i16 %loaded
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_cyclic_byte_lane_carry() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %body ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr %pointer, align 1
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_cyclic_byte_lane_alias() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %body ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr %pointer, align 1
  %extra = load i8, ptr %pointer, align 1
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_conditional_cyclic_byte_lane_carry(
    i1 %write) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %write, label %write_arm, label %carry_arm

write_arm:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr %pointer, align 1
  br label %latch

carry_arm:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_conditional_cyclic_byte_lane_alias() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %write_arm, label %carry_arm

write_arm:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr %pointer, align 1
  br label %latch

carry_arm:
  %extra = load i8, ptr %pointer, align 1
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_conditional_cyclic_byte_lane_carry(
    i1 %write) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %write, label %write_head, label %carry_head

write_head:
  %write_forward = add i8 1, 1
  br label %write_tail

write_tail:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr %pointer, align 1
  br label %latch

carry_head:
  %carry_forward = add i8 2, 1
  br label %carry_tail

carry_tail:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %write_head, label %carry_head

write_head:
  br i1 true, label %write_tail, label %detour

detour:
  br label %write_tail

write_tail:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr %pointer, align 1
  br label %latch

carry_head:
  br label %carry_tail

carry_tail:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_multiarm_conditional_cyclic_byte_lane_carry(
    i1 %write_low, i1 %write_high) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %write_low, label %write_low_arm, label %inner

inner:
  br i1 %write_high, label %write_high_arm, label %carry_arm

write_low_arm:
  %poisoned_low = add nsw i8 127, 1
  store i8 %poisoned_low, ptr %pointer, align 1
  br label %latch

write_high_arm:
  %poisoned_high = add nsw i8 127, 1
  store i8 %poisoned_high, ptr %high_pointer, align 1
  br label %latch

carry_arm:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_fourarm_conditional_cyclic_byte_lane() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %write_low_arm, label %inner

inner:
  br i1 true, label %write_high_arm, label %inner_second

inner_second:
  br i1 true, label %third_arm, label %carry_arm

write_low_arm:
  %poisoned_low = add nsw i8 127, 1
  store i8 %poisoned_low, ptr %pointer, align 1
  br label %latch

write_high_arm:
  br label %latch

third_arm:
  br label %latch

carry_arm:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_multiarm_conditional_cyclic_byte_lane_carry(
    i1 %write_low, i1 %write_high) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %write_low, label %write_low_head, label %inner

inner:
  br i1 %write_high, label %write_high_head, label %carry_head

write_low_head:
  %low_forward = add i8 1, 1
  br label %write_low_tail

write_low_tail:
  %poisoned_low = add nsw i8 127, 1
  store i8 %poisoned_low, ptr %pointer, align 1
  br label %latch

write_high_head:
  %high_forward = add i8 2, 1
  br label %write_high_tail

write_high_tail:
  %poisoned_high = add nsw i8 127, 1
  store i8 %poisoned_high, ptr %high_pointer, align 1
  br label %latch

carry_head:
  %carry_forward = add i8 3, 1
  br label %carry_tail

carry_tail:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_multiarm_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %write_low_head, label %inner

inner:
  br i1 true, label %write_high_tail, label %carry_tail

write_low_head:
  br i1 true, label %write_low_tail, label %detour

detour:
  br label %write_low_tail

write_low_tail:
  %poisoned_low = add nsw i8 127, 1
  store i8 %poisoned_low, ptr %pointer, align 1
  br label %latch

write_high_tail:
  br label %latch

carry_tail:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_recursive_conditional_cyclic_byte_lane_carry(
    i1 %write_first, i1 %write_second, i1 %write_third) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %write_first, label %first_arm, label %inner_first

inner_first:
  br i1 %write_second, label %second_arm, label %inner_second

inner_second:
  br i1 %write_third, label %third_arm, label %carry_arm

first_arm:
  %poisoned_first = add nsw i8 127, 1
  store i8 %poisoned_first, ptr %pointer, align 1
  br label %latch

second_arm:
  %poisoned_second = add nsw i8 127, 1
  store i8 %poisoned_second, ptr %high_pointer, align 1
  br label %latch

third_arm:
  %poisoned_third = sub nsw i8 -128, 1
  store i8 %poisoned_third, ptr %pointer, align 1
  br label %latch

carry_arm:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_recursive_conditional_cyclic_byte_lane_shared_leaf() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_arm, label %inner_first

inner_first:
  br i1 true, label %second_arm, label %inner_second

inner_second:
  br i1 true, label %third_arm, label %carry_arm

first_arm:
  %poisoned_first = add nsw i8 127, 1
  store i8 %poisoned_first, ptr %pointer, align 1
  br label %latch

second_arm:
  br label %latch

third_arm:
  br label %latch

carry_arm:
  br label %latch

extra:
  br label %third_arm

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_recursive_conditional_cyclic_byte_lane_carry(
    i1 %write_first, i1 %write_second, i1 %write_third) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %write_first, label %first_head, label %decision_forward

decision_forward:
  br label %inner_first

inner_first:
  br i1 %write_second, label %second_head, label %inner_second

inner_second:
  br i1 %write_third, label %third_head, label %carry_head

first_head:
  br label %first_tail

first_tail:
  %poisoned_first = add nsw i8 127, 1
  store i8 %poisoned_first, ptr %pointer, align 1
  br label %latch

second_head:
  br label %second_tail

second_tail:
  %poisoned_second = add nsw i8 127, 1
  store i8 %poisoned_second, ptr %high_pointer, align 1
  br label %latch

third_head:
  br label %third_tail

third_tail:
  %poisoned_third = sub nsw i8 -128, 1
  store i8 %poisoned_third, ptr %pointer, align 1
  br label %latch

carry_head:
  br label %carry_tail

carry_tail:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_recursive_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_head, label %inner_first

inner_first:
  br i1 true, label %second_tail, label %inner_second

inner_second:
  br i1 true, label %third_tail, label %carry_tail

first_head:
  br i1 true, label %first_tail, label %detour

detour:
  br label %first_tail

first_tail:
  %poisoned_first = add nsw i8 127, 1
  store i8 %poisoned_first, ptr %pointer, align 1
  br label %latch

second_tail:
  br label %latch

third_tail:
  br label %latch

carry_tail:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_grouped_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_group, i1 %group_side, i1 %write_other) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_group, label %group_branch, label %other_branch

group_branch:
  %group_poison = add nsw i8 127, 1
  store i8 %group_poison, ptr %pointer, align 1
  br i1 %group_side, label %group_leaf_first, label %group_leaf_second

other_branch:
  br i1 %write_other, label %other_store_leaf, label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %high_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_grouped_recursive_conditional_cyclic_byte_lane_two_groups() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_group, label %second_group

first_group:
  %first_poison = add nsw i8 127, 1
  store i8 %first_poison, ptr %pointer, align 1
  br i1 true, label %first_leaf, label %second_leaf

second_group:
  %second_poison = add nsw i8 127, 1
  store i8 %second_poison, ptr %high_pointer, align 1
  br i1 true, label %third_leaf, label %carry_leaf

first_leaf:
  br label %latch

second_leaf:
  br label %latch

third_leaf:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_repeated_source_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_group, i1 %group_first, i1 %group_second,
    i1 %write_other) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_group, label %group_branch, label %other_branch

group_branch:
  %group_poison = add nsw i8 127, 1
  store i8 %group_poison, ptr %pointer, align 1
  br i1 %group_first, label %group_leaf_first, label %group_inner

group_inner:
  br i1 %group_second, label %group_leaf_second, label %override_leaf

other_branch:
  br i1 %write_other, label %other_store_leaf, label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = sub nsw i8 -128, 1
  store i8 %override_poison, ptr %pointer, align 1
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %high_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_repeated_source_recursive_conditional_cyclic_byte_lane_mismatched_override() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %group_branch, label %other_branch

group_branch:
  %group_poison = add nsw i8 127, 1
  store i8 %group_poison, ptr %pointer, align 1
  br i1 false, label %group_leaf_first, label %group_inner

group_inner:
  br i1 false, label %group_leaf_second, label %override_leaf

other_branch:
  br i1 true, label %other_store_leaf, label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = add nsw i8 127, 1
  store i8 %override_poison, ptr %high_pointer, align 1
  br label %latch

other_store_leaf:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_composed_repeated_source_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_group, i1 %group_first, i1 %group_second,
    i1 %write_other) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %override_pointer = getelementptr i8, ptr %pointer, i64 1
  %other_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_group, label %group_branch, label %other_branch

group_branch:
  %group_poison = add nsw i16 32767, 1
  store i16 %group_poison, ptr %pointer, align 1
  br i1 %group_first, label %group_leaf_first, label %group_inner

group_inner:
  br i1 %group_second, label %group_leaf_second, label %override_leaf

other_branch:
  br i1 %write_other, label %other_store_leaf, label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = sub nsw i8 -128, 1
  store i8 %override_poison, ptr %override_pointer, align 1
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %other_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_composed_repeated_source_recursive_conditional_cyclic_byte_lane_shadowed_group() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %group_branch, label %other_branch

group_branch:
  %group_pointer = getelementptr i8, ptr %pointer, i64 1
  %group_poison = add nsw i8 127, 1
  store i8 %group_poison, ptr %group_pointer, align 1
  br i1 false, label %group_leaf_first, label %group_inner

group_inner:
  br i1 false, label %group_leaf_second, label %override_leaf

other_branch:
  br i1 true, label %other_store_leaf, label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = add nsw i16 32767, 1
  store i16 %override_poison, ptr %pointer, align 1
  br label %latch

other_store_leaf:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane(
    i1 %choose_group, i1 %group_first, i1 %group_second,
    i1 %write_other, i1 %carry_first) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %override_pointer = getelementptr i8, ptr %pointer, i64 1
  %other_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_group, label %group_branch, label %other_branch

group_branch:
  %group_poison = add nsw i16 32767, 1
  store i16 %group_poison, ptr %pointer, align 1
  br i1 %group_first, label %group_leaf_first, label %group_inner

group_inner:
  br i1 %group_second, label %group_leaf_second, label %override_leaf

other_branch:
  br i1 %write_other, label %other_store_leaf, label %carry_branch

carry_branch:
  br i1 %carry_first, label %carry_leaf_first, label %carry_leaf_second

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = sub nsw i8 -128, 1
  store i8 %override_poison, ptr %override_pointer, align 1
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %other_pointer, align 1
  br label %latch

carry_leaf_first:
  br label %latch

carry_leaf_second:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_shared_carry() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %override_pointer = getelementptr i8, ptr %pointer, i64 1
  %other_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %group_branch, label %other_branch

group_branch:
  %group_poison = add nsw i16 32767, 1
  store i16 %group_poison, ptr %pointer, align 1
  br i1 true, label %group_leaf_first, label %group_inner

group_inner:
  br i1 true, label %group_leaf_second, label %override_leaf

other_branch:
  br i1 true, label %other_store_leaf, label %carry_branch

carry_branch:
  br i1 true, label %carry_leaf, label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = sub nsw i8 -128, 1
  store i8 %override_poison, ptr %override_pointer, align 1
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %other_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_multigroup_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_first_group, i1 %first_leaf,
    i1 %choose_second_group, i1 %second_leaf) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_first_group, label %first_group, label %second_half

first_group:
  %first_poison = add nsw i8 127, 1
  store i8 %first_poison, ptr %pointer, align 1
  br i1 %first_leaf, label %first_leaf_a, label %first_leaf_b

second_half:
  br i1 %choose_second_group, label %second_group, label %carry_leaf

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %high_pointer, align 1
  br i1 %second_leaf, label %second_leaf_a, label %second_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_multigroup_recursive_conditional_cyclic_byte_lane_nested_groups() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_group, label %carry_leaf

first_group:
  %first_poison = add nsw i8 127, 1
  store i8 %first_poison, ptr %pointer, align 1
  br i1 false, label %first_leaf, label %second_group

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %high_pointer, align 1
  br i1 true, label %second_leaf_a, label %second_leaf_b

first_leaf:
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_mixed_multigroup_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_first_group, i1 %first_leaf, i1 %first_inner,
    i1 %choose_second_group, i1 %second_leaf) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %override_pointer = getelementptr i8, ptr %pointer, i64 1
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_first_group, label %first_group, label %second_half

first_group:
  %first_poison = add nsw i16 32767, 1
  store i16 %first_poison, ptr %pointer, align 1
  br i1 %first_leaf, label %first_leaf_a, label %first_inner_branch

first_inner_branch:
  br i1 %first_inner, label %first_leaf_b, label %override_leaf

second_half:
  br i1 %choose_second_group, label %second_group, label %carry_leaf

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 %second_leaf, label %second_leaf_a, label %second_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

override_leaf:
  %override_poison = add nsw i8 127, 1
  store i8 %override_poison, ptr %override_pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_mixed_multigroup_recursive_conditional_cyclic_byte_lane_shadowed_group() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %first_group_pointer = getelementptr i8, ptr %pointer, i64 1
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_group, label %second_half

first_group:
  %first_poison = add nsw i8 127, 1
  store i8 %first_poison, ptr %first_group_pointer, align 1
  br i1 false, label %first_leaf_a, label %first_inner_branch

first_inner_branch:
  br i1 false, label %first_leaf_b, label %override_leaf

second_half:
  br i1 true, label %second_group, label %carry_leaf

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 true, label %second_leaf_a, label %second_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

override_leaf:
  %override_poison = add nsw i16 32767, 1
  store i16 %override_poison, ptr %pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_grouped_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_group, i1 %group_side, i1 %write_other) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_group, label %group_entry, label %other_entry

group_entry:
  br label %group_branch

group_branch:
  %group_poison = add nsw i8 127, 1
  store i8 %group_poison, ptr %pointer, align 1
  br i1 %group_side, label %group_first_head, label %group_second_head

other_entry:
  br label %other_branch

other_branch:
  br i1 %write_other, label %other_store_head, label %carry_head

group_first_head:
  br label %group_leaf_first

group_second_head:
  br label %group_leaf_second

other_store_head:
  br label %other_store_leaf

carry_head:
  br label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %high_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_grouped_recursive_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %group_branch, label %other_branch

group_branch:
  %group_poison = add nsw i8 127, 1
  store i8 %group_poison, ptr %pointer, align 1
  br i1 true, label %group_first_head, label %group_leaf_second

group_first_head:
  br i1 true, label %group_leaf_first, label %group_detour

group_detour:
  br label %group_leaf_first

other_branch:
  br i1 true, label %other_store_leaf, label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %high_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_repeated_source_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_group, i1 %group_first, i1 %group_second,
    i1 %write_other) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_group, label %group_entry, label %other_entry

group_entry:
  br label %group_branch

group_branch:
  %group_poison = add nsw i8 127, 1
  store i8 %group_poison, ptr %pointer, align 1
  br i1 %group_first, label %group_first_head, label %group_inner_head

group_inner_head:
  br label %group_inner

group_inner:
  br i1 %group_second, label %group_second_head, label %override_head

other_entry:
  br label %other_branch

other_branch:
  br i1 %write_other, label %other_store_head, label %carry_head

group_first_head:
  br label %group_leaf_first

group_second_head:
  br label %group_leaf_second

override_head:
  br label %override_leaf

other_store_head:
  br label %other_store_leaf

carry_head:
  br label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = sub nsw i8 -128, 1
  store i8 %override_poison, ptr %pointer, align 1
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %high_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_repeated_source_recursive_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %group_branch, label %other_branch

group_branch:
  %group_poison = add nsw i8 127, 1
  store i8 %group_poison, ptr %pointer, align 1
  br i1 true, label %group_first_head, label %group_inner

group_first_head:
  br i1 true, label %group_leaf_first, label %group_detour

group_detour:
  br label %group_leaf_first

group_inner:
  br i1 false, label %group_leaf_second, label %override_leaf

other_branch:
  br i1 true, label %other_store_leaf, label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = sub nsw i8 -128, 1
  store i8 %override_poison, ptr %pointer, align 1
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %high_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_composed_repeated_source_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_group, i1 %group_first, i1 %group_second,
    i1 %write_other) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %override_pointer = getelementptr i8, ptr %pointer, i64 1
  %other_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_group, label %group_entry, label %other_entry

group_entry:
  br label %group_branch

group_branch:
  %group_poison = add nsw i16 32767, 1
  store i16 %group_poison, ptr %pointer, align 1
  br i1 %group_first, label %group_first_head, label %group_inner_head

group_inner_head:
  br label %group_inner

group_inner:
  br i1 %group_second, label %group_second_head, label %override_head

other_entry:
  br label %other_branch

other_branch:
  br i1 %write_other, label %other_store_head, label %carry_head

group_first_head:
  br label %group_leaf_first

group_second_head:
  br label %group_leaf_second

override_head:
  br label %override_leaf

other_store_head:
  br label %other_store_leaf

carry_head:
  br label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = sub nsw i8 -128, 1
  store i8 %override_poison, ptr %override_pointer, align 1
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %other_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_composed_repeated_source_recursive_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %override_pointer = getelementptr i8, ptr %pointer, i64 1
  %other_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %group_branch, label %other_branch

group_branch:
  %group_poison = add nsw i16 32767, 1
  store i16 %group_poison, ptr %pointer, align 1
  br i1 true, label %group_first_head, label %group_inner

group_first_head:
  br i1 true, label %group_leaf_first, label %group_detour

group_detour:
  br label %group_leaf_first

group_inner:
  br i1 false, label %group_leaf_second, label %override_leaf

other_branch:
  br i1 true, label %other_store_leaf, label %carry_leaf

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = sub nsw i8 -128, 1
  store i8 %override_poison, ptr %override_pointer, align 1
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %other_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_multigroup_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_first_group, i1 %first_leaf,
    i1 %choose_second_group, i1 %second_leaf) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_first_group, label %first_group_head, label %second_half_head

first_group_head:
  br label %first_group

first_group:
  %first_poison = add nsw i8 127, 1
  store i8 %first_poison, ptr %pointer, align 1
  br i1 %first_leaf, label %first_leaf_a_head, label %first_leaf_b_head

second_half_head:
  br label %second_half

second_half:
  br i1 %choose_second_group, label %second_group_head, label %carry_head

second_group_head:
  br label %second_group

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %high_pointer, align 1
  br i1 %second_leaf, label %second_leaf_a_head, label %second_leaf_b_head

first_leaf_a_head:
  br label %first_leaf_a

first_leaf_b_head:
  br label %first_leaf_b

second_leaf_a_head:
  br label %second_leaf_a

second_leaf_b_head:
  br label %second_leaf_b

carry_head:
  br label %carry_leaf

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_multigroup_recursive_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %high_pointer = getelementptr i8, ptr %pointer, i64 1
  store i16 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i16, ptr %pointer, align 1
  %stable = freeze i16 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_group, label %second_half

first_group:
  %first_poison = add nsw i8 127, 1
  store i8 %first_poison, ptr %pointer, align 1
  br i1 true, label %first_leaf_head, label %first_leaf_b

first_leaf_head:
  br i1 true, label %first_leaf_a, label %first_detour

first_detour:
  br label %first_leaf_a

second_half:
  br i1 true, label %second_group, label %carry_leaf

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %high_pointer, align 1
  br i1 true, label %second_leaf_a, label %second_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i16 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_mixed_multigroup_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_first_group, i1 %first_leaf, i1 %first_inner,
    i1 %choose_second_group, i1 %second_leaf) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %override_pointer = getelementptr i8, ptr %pointer, i64 1
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_first_group, label %first_group_head, label %second_half

first_group_head:
  br label %first_group

first_group:
  %first_poison = add nsw i16 32767, 1
  store i16 %first_poison, ptr %pointer, align 1
  br i1 %first_leaf, label %first_leaf_a, label %first_inner_branch

first_inner_branch:
  br i1 %first_inner, label %first_leaf_b, label %override_leaf

second_half:
  br i1 %choose_second_group, label %second_group, label %carry_leaf

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 %second_leaf, label %second_leaf_a, label %second_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

override_leaf:
  %override_poison = add nsw i8 127, 1
  store i8 %override_poison, ptr %override_pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_mixed_multigroup_recursive_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %override_pointer = getelementptr i8, ptr %pointer, i64 1
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_group_head, label %second_half

first_group_head:
  br i1 true, label %first_group, label %first_group_detour

first_group_detour:
  br label %first_group

first_group:
  %first_poison = add nsw i16 32767, 1
  store i16 %first_poison, ptr %pointer, align 1
  br i1 false, label %first_leaf_a, label %first_inner_branch

first_inner_branch:
  br i1 false, label %first_leaf_b, label %override_leaf

second_half:
  br i1 true, label %second_group, label %carry_leaf

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 true, label %second_leaf_a, label %second_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

override_leaf:
  %override_poison = add nsw i8 127, 1
  store i8 %override_poison, ptr %override_pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane(
    i1 %choose_group, i1 %group_first, i1 %group_second,
    i1 %write_other, i1 %carry_first) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %override_pointer = getelementptr i8, ptr %pointer, i64 1
  %other_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_group, label %group_head, label %other_branch

group_head:
  br label %group_branch

group_branch:
  %group_poison = add nsw i16 32767, 1
  store i16 %group_poison, ptr %pointer, align 1
  br i1 %group_first, label %group_leaf_first, label %group_inner

group_inner:
  br i1 %group_second, label %group_leaf_second, label %override_leaf

other_branch:
  br i1 %write_other, label %other_store_leaf, label %carry_branch

carry_branch:
  br i1 %carry_first, label %carry_leaf_first, label %carry_leaf_second

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = sub nsw i8 -128, 1
  store i8 %override_poison, ptr %override_pointer, align 1
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %other_pointer, align 1
  br label %latch

carry_leaf_first:
  br label %latch

carry_leaf_second:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %override_pointer = getelementptr i8, ptr %pointer, i64 1
  %other_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %group_head, label %other_branch

group_head:
  br i1 true, label %group_branch, label %group_detour

group_detour:
  br label %group_branch

group_branch:
  %group_poison = add nsw i16 32767, 1
  store i16 %group_poison, ptr %pointer, align 1
  br i1 false, label %group_leaf_first, label %group_inner

group_inner:
  br i1 false, label %group_leaf_second, label %override_leaf

other_branch:
  br i1 true, label %other_store_leaf, label %carry_branch

carry_branch:
  br i1 true, label %carry_leaf_first, label %carry_leaf_second

group_leaf_first:
  br label %latch

group_leaf_second:
  br label %latch

override_leaf:
  %override_poison = sub nsw i8 -128, 1
  store i8 %override_poison, ptr %override_pointer, align 1
  br label %latch

other_store_leaf:
  %other_poison = add nsw i8 127, 1
  store i8 %other_poison, ptr %other_pointer, align 1
  br label %latch

carry_leaf_first:
  br label %latch

carry_leaf_second:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_trigroup_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_first, i1 %first_leaf, i1 %choose_second,
    i1 %second_leaf, i1 %choose_third, i1 %third_leaf) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %second_pointer = getelementptr i8, ptr %pointer, i64 1
  %third_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_first, label %first_group, label %second_tail

first_group:
  %first_poison = add nsw i8 127, 1
  store i8 %first_poison, ptr %pointer, align 1
  br i1 %first_leaf, label %first_leaf_a, label %first_leaf_b

second_tail:
  br i1 %choose_second, label %second_group, label %third_tail

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 %second_leaf, label %second_leaf_a, label %second_leaf_b

third_tail:
  br i1 %choose_third, label %third_group, label %carry_leaf

third_group:
  %third_poison = add nsw i8 127, 1
  store i8 %third_poison, ptr %third_pointer, align 1
  br i1 %third_leaf, label %third_leaf_a, label %third_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

third_leaf_a:
  br label %latch

third_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_trigroup_recursive_conditional_cyclic_byte_lane_nested_groups() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %second_pointer = getelementptr i8, ptr %pointer, i64 1
  %third_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_group, label %third_tail

first_group:
  %first_poison = add nsw i8 127, 1
  store i8 %first_poison, ptr %pointer, align 1
  br i1 false, label %first_leaf_a, label %second_group

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 true, label %second_leaf_a, label %second_leaf_b

third_tail:
  br i1 true, label %third_group, label %carry_leaf

third_group:
  %third_poison = add nsw i8 127, 1
  store i8 %third_poison, ptr %third_pointer, align 1
  br i1 true, label %third_leaf_a, label %third_leaf_b

first_leaf_a:
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

third_leaf_a:
  br label %latch

third_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_first, i1 %first_leaf, i1 %first_inner,
    i1 %choose_second, i1 %second_leaf, i1 %second_inner) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %first_override_pointer = getelementptr i8, ptr %pointer, i64 1
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  %second_override_pointer = getelementptr i8, ptr %pointer, i64 3
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_first, label %first_group, label %second_tail

first_group:
  %first_poison = add nsw i16 32767, 1
  store i16 %first_poison, ptr %pointer, align 1
  br i1 %first_leaf, label %first_leaf_a, label %first_inner_branch

first_inner_branch:
  br i1 %first_inner, label %first_leaf_b, label %first_override_leaf

second_tail:
  br i1 %choose_second, label %second_group, label %carry_leaf

second_group:
  %second_poison = sub nsw i16 -32768, 1
  store i16 %second_poison, ptr %second_pointer, align 1
  br i1 %second_leaf, label %second_leaf_a, label %second_inner_branch

second_inner_branch:
  br i1 %second_inner, label %second_leaf_b, label %second_override_leaf

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

first_override_leaf:
  %first_override_poison = add nsw i8 127, 1
  store i8 %first_override_poison, ptr %first_override_pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

second_override_leaf:
  %second_override_poison = sub nsw i8 -128, 1
  store i8 %second_override_poison, ptr %second_override_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_same_group() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %first_override_pointer = getelementptr i8, ptr %pointer, i64 1
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_group, label %second_tail

first_group:
  %first_poison = add nsw i16 32767, 1
  store i16 %first_poison, ptr %pointer, align 1
  br i1 false, label %first_pure_branch, label %first_override_branch

first_pure_branch:
  br i1 false, label %first_leaf_a, label %first_leaf_b

first_override_branch:
  br i1 false, label %first_override_leaf_a, label %first_override_leaf_b

second_tail:
  br i1 true, label %second_group, label %carry_leaf

second_group:
  %second_poison = sub nsw i16 -32768, 1
  store i16 %second_poison, ptr %second_pointer, align 1
  br i1 true, label %second_leaf_a, label %second_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

first_override_leaf_a:
  %first_override_poison_a = add nsw i8 127, 1
  store i8 %first_override_poison_a, ptr %first_override_pointer, align 1
  br label %latch

first_override_leaf_b:
  %first_override_poison_b = sub nsw i8 -128, 1
  store i8 %first_override_poison_b, ptr %first_override_pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_first, i1 %first_leaf, i1 %first_inner,
    i1 %choose_second, i1 %second_leaf, i1 %second_inner) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %first_override_pointer = getelementptr i8, ptr %pointer, i64 1
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  %second_override_pointer = getelementptr i8, ptr %pointer, i64 3
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_first, label %first_group_head, label %second_tail

first_group_head:
  br label %first_group

first_group:
  %first_poison = add nsw i16 32767, 1
  store i16 %first_poison, ptr %pointer, align 1
  br i1 %first_leaf, label %first_leaf_a, label %first_inner_branch

first_inner_branch:
  br i1 %first_inner, label %first_leaf_b, label %first_override_leaf

second_tail:
  br i1 %choose_second, label %second_group, label %carry_leaf

second_group:
  %second_poison = sub nsw i16 -32768, 1
  store i16 %second_poison, ptr %second_pointer, align 1
  br i1 %second_leaf, label %second_leaf_a, label %second_inner_branch

second_inner_branch:
  br i1 %second_inner, label %second_leaf_b, label %second_override_leaf

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

first_override_leaf:
  %first_override_poison = add nsw i8 127, 1
  store i8 %first_override_poison, ptr %first_override_pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

second_override_leaf:
  %second_override_poison = sub nsw i8 -128, 1
  store i8 %second_override_poison, ptr %second_override_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %first_override_pointer = getelementptr i8, ptr %pointer, i64 1
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  %second_override_pointer = getelementptr i8, ptr %pointer, i64 3
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_group_head, label %second_tail

first_group_head:
  br i1 true, label %first_group, label %first_group_detour

first_group_detour:
  br label %first_group

first_group:
  %first_poison = add nsw i16 32767, 1
  store i16 %first_poison, ptr %pointer, align 1
  br i1 false, label %first_leaf_a, label %first_inner_branch

first_inner_branch:
  br i1 false, label %first_leaf_b, label %first_override_leaf

second_tail:
  br i1 true, label %second_group, label %carry_leaf

second_group:
  %second_poison = sub nsw i16 -32768, 1
  store i16 %second_poison, ptr %second_pointer, align 1
  br i1 false, label %second_leaf_a, label %second_inner_branch

second_inner_branch:
  br i1 false, label %second_leaf_b, label %second_override_leaf

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

first_override_leaf:
  %first_override_poison = add nsw i8 127, 1
  store i8 %first_override_poison, ptr %first_override_pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

second_override_leaf:
  %second_override_poison = sub nsw i8 -128, 1
  store i8 %second_override_poison, ptr %second_override_pointer, align 1
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_trigroup_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_first, i1 %first_leaf, i1 %choose_second,
    i1 %second_leaf, i1 %choose_third, i1 %third_leaf) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %second_pointer = getelementptr i8, ptr %pointer, i64 1
  %third_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_first, label %first_group_head, label %second_tail

first_group_head:
  br label %first_group

first_group:
  %first_poison = add nsw i8 127, 1
  store i8 %first_poison, ptr %pointer, align 1
  br i1 %first_leaf, label %first_leaf_a, label %first_leaf_b

second_tail:
  br i1 %choose_second, label %second_group, label %third_tail

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 %second_leaf, label %second_leaf_a, label %second_leaf_b

third_tail:
  br i1 %choose_third, label %third_group, label %carry_leaf

third_group:
  %third_poison = add nsw i8 127, 1
  store i8 %third_poison, ptr %third_pointer, align 1
  br i1 %third_leaf, label %third_leaf_a, label %third_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

third_leaf_a:
  br label %latch

third_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_trigroup_recursive_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %second_pointer = getelementptr i8, ptr %pointer, i64 1
  %third_pointer = getelementptr i8, ptr %pointer, i64 2
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_group_head, label %second_tail

first_group_head:
  br i1 true, label %first_group, label %first_group_detour

first_group_detour:
  br label %first_group

first_group:
  %first_poison = add nsw i8 127, 1
  store i8 %first_poison, ptr %pointer, align 1
  br i1 true, label %first_leaf_a, label %first_leaf_b

second_tail:
  br i1 true, label %second_group, label %third_tail

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 true, label %second_leaf_a, label %second_leaf_b

third_tail:
  br i1 true, label %third_group, label %carry_leaf

third_group:
  %third_poison = add nsw i8 127, 1
  store i8 %third_poison, ptr %third_pointer, align 1
  br i1 true, label %third_leaf_a, label %third_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

third_leaf_a:
  br label %latch

third_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_canonical_address() {
entry:
  %poisoned = add nsw i8 127, 1
  %store_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 2
  store i8 %poisoned, ptr %store_pointer, align 1
  %array_base = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %load_pointer = getelementptr i8, ptr %array_base, i64 2
  %loaded = load i8, ptr %load_pointer, align 1
  store i8 0, ptr %store_pointer, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_cross_block() {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %transit

transit:
  br label %consume

consume:
  %loaded = load i8, ptr @poison_memory_slot, align 1
  store i8 0, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_cross_block_initial_merge() {
entry:
  br i1 true, label %poison_path, label %alternate

poison_path:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %merge

alternate:
  br label %merge

merge:
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define internal i8 @cross_function_poison_source() {
entry:
  %poisoned = add nsw i8 127, 1
  ret i8 %poisoned
}

define i8 @freeze_cross_function_poison() {
entry:
  %poisoned = call i8 @cross_function_poison_source()
  %stable = freeze i8 %poisoned
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define internal i8 @cross_function_defined_source() {
entry:
  %sum = add nsw i8 1, 1
  ret i8 %sum
}

define i8 @freeze_cross_function_defined() {
entry:
  %sum = call i8 @cross_function_defined_source()
  %stable = freeze i8 %sum
  ret i8 %stable
}

define internal i8 @cross_function_multiple_source() {
entry:
  %poisoned = add nsw i8 127, 1
  ret i8 %poisoned
}

define i8 @bad_freeze_cross_function_multiple_calls() {
entry:
  %first = call i8 @cross_function_multiple_source()
  %second = call i8 @cross_function_multiple_source()
  %stable = freeze i8 %second
  %combined = xor i8 %first, %stable
  ret i8 %combined
}

define internal i8 @cross_function_argument_sink(i8 %value) {
entry:
  %stable = freeze i8 %value
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_cross_function_argument() {
entry:
  %poisoned = add nsw i8 127, 1
  %result = call i8 @cross_function_argument_sink(i8 %poisoned)
  ret i8 %result
}

define internal i8 @cross_function_argument_defined_sink(i8 %value) {
entry:
  %stable = freeze i8 %value
  ret i8 %stable
}

define i8 @freeze_cross_function_argument_defined() {
entry:
  %sum = add nsw i8 1, 1
  %result = call i8 @cross_function_argument_defined_sink(i8 %sum)
  ret i8 %result
}

define internal i8 @cross_function_argument_multiple_sink(i8 %value) {
entry:
  %stable = freeze i8 %value
  ret i8 %stable
}

define i8 @freeze_cross_function_argument_multiple_calls() {
entry:
  %poisoned = add nsw i8 127, 1
  %first = call i8 @cross_function_argument_multiple_sink(i8 %poisoned)
  %second = call i8 @cross_function_argument_multiple_sink(i8 7)
  %matched = icmp eq i8 %first, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define internal i8 @cross_function_return_multiple_source() {
entry:
  %poisoned = add nsw i8 127, 1
  ret i8 %poisoned
}

define i8 @freeze_cross_function_return_multiple_calls() {
entry:
  %first = call i8 @cross_function_return_multiple_source()
  %first_stable = freeze i8 %first
  %second = call i8 @cross_function_return_multiple_source()
  %second_stable = freeze i8 %second
  %matched = icmp eq i8 %first_stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define internal i8 @cross_function_argument_return_passthrough(
    i8 %value) {
entry:
  %forwarded = xor i8 %value, 0
  ret i8 %forwarded
}

define i8 @freeze_cross_function_argument_return_passthrough() {
entry:
  %poisoned = add nsw i8 127, 1
  %first = call i8 @cross_function_argument_return_passthrough(
      i8 %poisoned)
  %first_stable = freeze i8 %first
  %second = call i8 @cross_function_argument_return_passthrough(i8 7)
  %second_stable = freeze i8 %second
  %matched = icmp eq i8 %first_stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define internal i8 @cross_function_uncomposed_poison(i8 %value) {
entry:
  %argument_stable = freeze i8 %value
  %local_poison = add nsw i8 127, 1
  ret i8 %local_poison
}

define i8 @freeze_cross_function_uncomposed_poison() {
entry:
  %argument_poison = add nsw i8 127, 1
  %result = call i8 @cross_function_uncomposed_poison(
      i8 %argument_poison)
  %result_stable = freeze i8 %result
  %matched = icmp eq i8 %result_stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_distinct_address() {
entry:
  %poisoned = add nsw i8 127, 1
  %store_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 1
  store i8 %poisoned, ptr %store_pointer, align 1
  %array_base = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %load_pointer = getelementptr i8, ptr %array_base, i64 2
  %loaded = load i8, ptr %load_pointer, align 1
  %stable = freeze i8 %loaded
  ret i8 %stable
}

define i8 @bad_freeze_memory_multi_load() {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  %first = load i8, ptr @poison_memory_slot, align 1
  %second = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %second
  %result = xor i8 %first, %stable
  ret i8 %result
}

define i8 @freeze_memory_multiple_load_sinks() {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  %first = load i8, ptr @poison_memory_slot, align 1
  %second = load i8, ptr @poison_memory_slot, align 1
  store i8 0, ptr @poison_memory_slot, align 1
  %first_stable = freeze i8 %first
  %second_stable = freeze i8 %second
  %first_match = icmp eq i8 %first_stable, 42
  %second_match = icmp eq i8 %second_stable, 43
  %both_match = and i1 %first_match, %second_match
  br i1 %both_match, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_diamond(i8 %selector) {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  %take_left = icmp eq i8 %selector, 0
  br i1 %take_left, label %left, label %right

left:
  br label %merge

right:
  br label %merge

merge:
  %loaded = load i8, ptr @poison_memory_slot, align 1
  store i8 0, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_mixed_clobber(i8 %selector) {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  %take_left = icmp eq i8 %selector, 0
  br i1 %take_left, label %left, label %right

left:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %merge

right:
  br label %merge

merge:
  %loaded = load i8, ptr @poison_memory_slot, align 1
  store i8 0, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_initial_definition(i8 %selector) {
entry:
  %take_left = icmp eq i8 %selector, 0
  br i1 %take_left, label %poison_path, label %missing_path

poison_path:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %merge

missing_path:
  br label %merge

merge:
  %loaded = load i8, ptr @poison_memory_slot, align 1
  store i8 0, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_initial_subobject(i8 %selector) {
entry:
  %take_left = icmp eq i8 %selector, 0
  br i1 %take_left, label %poison_path, label %initial_path

poison_path:
  %store_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 2
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr %store_pointer, align 1
  br label %merge

initial_path:
  br label %merge

merge:
  %load_pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 2
  %loaded = load i8, ptr %load_pointer, align 1
  store i8 0, ptr %load_pointer, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_definedness_phi(i8 %selector) {
entry:
  %take_left = icmp eq i8 %selector, 0
  br i1 %take_left, label %poison_path, label %defined_path

poison_path:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %merge

defined_path:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %merge

merge:
  %loaded = load i8, ptr @poison_memory_slot, align 1
  store i8 0, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define internal i8 @memory_argument_phi_callee(
    i8 %value, i8 %selector) {
entry:
  %take_left = icmp eq i8 %selector, 0
  br i1 %take_left, label %poison_path, label %defined_path

poison_path:
  store i8 %value, ptr @poison_memory_slot, align 1
  br label %merge

defined_path:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %merge

merge:
  %loaded = load i8, ptr @poison_memory_slot, align 1
  store i8 0, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_argument_phi(i8 %selector) {
entry:
  %poisoned = add nsw i8 127, 1
  %result = call i8 @memory_argument_phi_callee(
      i8 %poisoned, i8 %selector)
  ret i8 %result
}

define internal i8 @memory_return_phi_source() {
entry:
  %poisoned = add nsw i8 127, 1
  ret i8 %poisoned
}

define i8 @freeze_memory_return_phi(i8 %selector) {
entry:
  %returned = call i8 @memory_return_phi_source()
  %take_left = icmp eq i8 %selector, 0
  br i1 %take_left, label %poison_path, label %defined_path

poison_path:
  store i8 %returned, ptr @poison_memory_slot, align 1
  br label %merge

defined_path:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %merge

merge:
  %loaded = load i8, ptr @poison_memory_slot, align 1
  store i8 0, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_multilevel_phi(i8 %selector) {
entry:
  %take_left = icmp eq i8 %selector, 0
  br i1 %take_left, label %poison_store, label %defined_store

poison_store:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %poison_forward

poison_forward:
  br label %poison_edge

poison_edge:
  br label %merge

defined_store:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %defined_forward

defined_forward:
  br label %merge

merge:
  %loaded = load i8, ptr @poison_memory_slot, align 1
  store i8 0, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_cyclic_phi() {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %remembered = phi i8 [ 0, %entry ], [ %stable, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %latch

latch:
  store i8 7, ptr @poison_memory_slot, align 1
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %remembered, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_cycle_carry() {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_conditional_cycle_carry(
    i8 %selector) {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %write_path, label %carry_path

write_path:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_conditional_cycle_carry(
    i8 %selector) {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %write_path, label %carry_path

write_path:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %write_forward

write_forward:
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_multiarm_conditional_cycle_carry(
    i8 %selector, i8 %route) {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %write_path, label %carry_path

write_path:
  store i8 7, ptr @poison_memory_slot, align 1
  %take_first = icmp eq i8 %route, 0
  br i1 %take_first, label %write_forward_a, label %write_forward_b

write_forward_a:
  br label %latch

write_forward_b:
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_equivalent_defined_store_cycle_carry(
    i8 %selector, i8 %route) {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %route_store, label %carry_path

route_store:
  %take_first = icmp eq i8 %route, 0
  br i1 %take_first, label %store_a, label %store_b

store_a:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %latch

store_b:
  store i8 8, ptr @poison_memory_slot, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_mixed_defined_poison_store_cycle_carry(
    i8 %selector, i8 %route) {
entry:
  %poisoned = add nsw i8 127, 1
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %route_store, label %carry_path

route_store:
  %take_first = icmp eq i8 %route, 0
  br i1 %take_first, label %store_a, label %store_b

store_a:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %latch

store_b:
  %poisoned_again = add nsw i8 127, 1
  store i8 %poisoned_again, ptr @poison_memory_slot, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  ret i8 %stable
}

define i8 @freeze_memory_shared_poison_store_cycle_carry(
    i8 %selector, i8 %route) {
entry:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %poisoned = add nsw i8 127, 1
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %route_store, label %carry_path

route_store:
  %take_first = icmp eq i8 %route, 0
  br i1 %take_first, label %store_a, label %store_b

store_a:
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %latch

store_b:
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_nested_poison_store_cycle_carry(
    i8 %selector, i8 %route) {
entry:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %take_first = icmp eq i8 %route, 0
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %route_store, label %carry_path

route_store:
  br i1 %take_first, label %store_a, label %store_b

store_a:
  store i8 %poisoned_a, ptr @poison_memory_slot, align 1
  br label %latch

store_b:
  store i8 %poisoned_b, ptr @poison_memory_slot, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_three_store_cycle_carry(
    i8 %selector, i8 %route, i8 %route_second) {
entry:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %route_store, label %carry_path

route_store:
  %take_first = icmp eq i8 %route, 0
  br i1 %take_first, label %store_a, label %route_more

route_more:
  %take_second = icmp eq i8 %route_second, 0
  br i1 %take_second, label %store_b, label %store_c

store_a:
  %poisoned_a = add nsw i8 127, 1
  store i8 %poisoned_a, ptr @poison_memory_slot, align 1
  br label %latch

store_b:
  %poisoned_b = sub nsw i8 -128, 1
  store i8 %poisoned_b, ptr @poison_memory_slot, align 1
  br label %latch

store_c:
  %poisoned_c = mul nsw i8 64, 2
  store i8 %poisoned_c, ptr @poison_memory_slot, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_recursive_poison_store_cycle_carry(
    i8 %selector, i8 %route, i8 %route_second) {
entry:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %poisoned_c = mul nsw i8 64, 2
  %take_first = icmp eq i8 %route, 0
  %take_second = icmp eq i8 %route_second, 0
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %route_store, label %carry_path

route_store:
  br i1 %take_first, label %store_a, label %route_more

route_more:
  br i1 %take_second, label %store_b, label %store_c

store_a:
  store i8 %poisoned_a, ptr @poison_memory_slot, align 1
  br label %latch

store_b:
  store i8 %poisoned_b, ptr @poison_memory_slot, align 1
  br label %latch

store_c:
  store i8 %poisoned_c, ptr @poison_memory_slot, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_grouped_recursive_poison_store_cycle_carry(
    i8 %selector, i8 %route, i8 %route_second, i8 %split) {
entry:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %poisoned_c = mul nsw i8 64, 2
  %take_first = icmp eq i8 %route, 0
  %take_second = icmp eq i8 %route_second, 0
  %split_first = icmp eq i8 %split, 0
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %route_store, label %carry_path

route_store:
  br i1 %take_first, label %store_a, label %route_more

route_more:
  br i1 %take_second, label %store_b, label %store_c

store_a:
  store i8 %poisoned_a, ptr @poison_memory_slot, align 1
  br i1 %split_first, label %store_a_left, label %store_a_right

store_a_left:
  br label %latch

store_a_right:
  br label %latch

store_b:
  store i8 %poisoned_b, ptr @poison_memory_slot, align 1
  br label %latch

store_c:
  store i8 %poisoned_c, ptr @poison_memory_slot, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_repeated_source_recursive_poison_store_cycle_carry(
    i8 %selector, i8 %route, i8 %route_second, i8 %route_third) {
entry:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %poisoned_shared = add nsw i8 127, 1
  %poisoned_c = sub nsw i8 -128, 1
  %poisoned_d = mul nsw i8 64, 2
  %take_first = icmp eq i8 %route, 0
  %take_second = icmp eq i8 %route_second, 0
  %take_third = icmp eq i8 %route_third, 0
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %route_store, label %carry_path

route_store:
  br i1 %take_first, label %store_a, label %route_more

route_more:
  br i1 %take_second, label %store_b, label %route_last

route_last:
  br i1 %take_third, label %store_c, label %store_d

store_a:
  store i8 %poisoned_shared, ptr @poison_memory_slot, align 1
  br label %latch

store_b:
  store i8 %poisoned_shared, ptr @poison_memory_slot, align 1
  br label %latch

store_c:
  store i8 %poisoned_c, ptr @poison_memory_slot, align 1
  br label %latch

store_d:
  store i8 %poisoned_d, ptr @poison_memory_slot, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_multicarry_recursive_poison_store_cycle(
    i8 %route, i8 %route_second, i8 %route_third, i8 %route_fourth) {
entry:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded = load i8, ptr @poison_memory_slot, align 1
  %stable = freeze i8 %loaded
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %poisoned_c = mul nsw i8 64, 2
  %take_first = icmp eq i8 %route, 0
  %take_second = icmp eq i8 %route_second, 0
  %take_third = icmp eq i8 %route_third, 0
  %take_fourth = icmp eq i8 %route_fourth, 0
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %route_first

route_first:
  br i1 %take_first, label %store_a, label %route_more

route_more:
  br i1 %take_second, label %carry_a, label %route_last

route_last:
  br i1 %take_third, label %store_b, label %route_final

route_final:
  br i1 %take_fourth, label %carry_b, label %store_c

store_a:
  store i8 %poisoned_a, ptr @poison_memory_slot, align 1
  br label %latch

store_b:
  store i8 %poisoned_b, ptr @poison_memory_slot, align 1
  br label %latch

store_c:
  store i8 %poisoned_c, ptr @poison_memory_slot, align 1
  br label %latch

carry_a:
  br label %latch

carry_b:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_multicell_cycle_carry(i8 %selector) {
entry:
  store i8 7, ptr @poison_memory_array, align 1
  store i8 7, ptr getelementptr inbounds ([4 x i8],
      ptr @poison_memory_array, i64 0, i64 1), align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded_a = load i8, ptr @poison_memory_array, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr getelementptr inbounds ([4 x i8],
      ptr @poison_memory_array, i64 0, i64 1), align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr @poison_memory_array, align 1
  store i8 %poisoned_b, ptr getelementptr inbounds ([4 x i8],
      ptr @poison_memory_array, i64 0, i64 1), align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_identified_object_multicell_cycle_carry(
    i8 %selector) {
entry:
  %object_a = alloca [1 x i8], align 1
  %object_b = alloca [1 x i8], align 1
  %cell_a = getelementptr inbounds [1 x i8], ptr %object_a,
      i64 0, i64 0
  %cell_b = getelementptr inbounds [1 x i8], ptr %object_b,
      i64 0, i64 0
  store i8 7, ptr %cell_a, align 1
  store i8 7, ptr %cell_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded_a = load i8, ptr %cell_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %cell_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %cell_a, align 1
  store i8 %poisoned_b, ptr %cell_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_fixed_heap_object_multicell_cycle_carry(
    i8 %selector) {
entry:
  %object_a = call ptr @malloc(i64 1)
  %object_b = call ptr @malloc(i64 1)
  store i8 7, ptr %object_a, align 1
  store i8 7, ptr %object_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded_a = load i8, ptr %object_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %object_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %object_a, align 1
  store i8 %poisoned_b, ptr %object_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_dynamic_heap_object_multicell_cycle_carry(
    i8 %selector) {
entry:
  %wide = zext i8 %selector to i64
  %size = or i64 %wide, 1
  %object_a = call ptr @malloc(i64 %size)
  %object_b = call ptr @malloc(i64 %size)
  store i8 7, ptr %object_a, align 1
  store i8 7, ptr %object_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded_a = load i8, ptr %object_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %object_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %object_a, align 1
  store i8 %poisoned_b, ptr %object_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_finite_pointer_domain_multicell_cycle_carry(
    i8 %selector) {
entry:
  %cell_0 = getelementptr inbounds [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %cell_1 = getelementptr inbounds [4 x i8],
      ptr @poison_memory_array, i64 0, i64 1
  %cell_2 = getelementptr inbounds [4 x i8],
      ptr @poison_memory_array, i64 0, i64 2
  %cell_3 = getelementptr inbounds [4 x i8],
      ptr @poison_memory_array, i64 0, i64 3
  %choose_pointer = icmp eq i8 %selector, 0
  %pointer_a = select i1 %choose_pointer, ptr %cell_0, ptr %cell_1
  %pointer_b = select i1 %choose_pointer, ptr %cell_2, ptr %cell_3
  store i8 7, ptr %pointer_a, align 1
  store i8 7, ptr %pointer_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded_a = load i8, ptr %pointer_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %pointer_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %pointer_a, align 1
  store i8 %poisoned_b, ptr %pointer_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_finite_pointer_phi_domain_multicell_cycle_carry(
    i8 %selector) {
entry:
  %choose_pointer = icmp eq i8 %selector, 0
  br i1 %choose_pointer, label %left, label %right

left:
  br label %initialize

right:
  br label %initialize

initialize:
  %pointer_a = phi ptr [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 0), %left ], [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 1), %right ]
  %pointer_b = phi ptr [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 2), %left ], [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 3), %right ]
  store i8 7, ptr %pointer_a, align 1
  store i8 7, ptr %pointer_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %initialize ], [ %next, %latch ]
  %loaded_a = load i8, ptr %pointer_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %pointer_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %pointer_a, align 1
  store i8 %poisoned_b, ptr %pointer_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_guard_correlated_pointer_domain_multicell_cycle_carry(
    i8 %selector) {
entry:
  %cell_0 = getelementptr inbounds [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %cell_1 = getelementptr inbounds [4 x i8],
      ptr @poison_memory_array, i64 0, i64 1
  %cell_2 = getelementptr inbounds [4 x i8],
      ptr @poison_memory_array, i64 0, i64 2
  %choose_pointer = icmp eq i8 %selector, 0
  %pointer_a = select i1 %choose_pointer, ptr %cell_0, ptr %cell_1
  %pointer_b = select i1 %choose_pointer, ptr %cell_1, ptr %cell_2
  store i8 7, ptr %pointer_a, align 1
  store i8 7, ptr %pointer_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded_a = load i8, ptr %pointer_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %pointer_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %pointer_a, align 1
  store i8 %poisoned_b, ptr %pointer_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_compatible_overlap_pointer_domain_cycle_carry(
    i8 %selector) {
entry:
  %cell_0 = getelementptr inbounds [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %cell_1 = getelementptr inbounds [4 x i8],
      ptr @poison_memory_array, i64 0, i64 1
  %cell_2 = getelementptr inbounds [4 x i8],
      ptr @poison_memory_array, i64 0, i64 2
  %choose_pointer = icmp eq i8 %selector, 0
  %pointer_a = select i1 %choose_pointer, ptr %cell_0, ptr %cell_1
  %pointer_b = select i1 %choose_pointer, ptr %cell_0, ptr %cell_2
  store i8 7, ptr %pointer_a, align 1
  store i8 7, ptr %pointer_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded_a = load i8, ptr %pointer_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %pointer_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %pointer_a, align 1
  store i8 %poisoned_b, ptr %pointer_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_phi_correlated_pointer_domain_cycle_carry(
    i8 %selector) {
entry:
  %choose_pointer = icmp eq i8 %selector, 0
  br i1 %choose_pointer, label %left, label %right

left:
  br label %initialize

right:
  br label %initialize

initialize:
  %pointer_a = phi ptr [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 0), %left ], [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 1), %right ]
  %pointer_b = phi ptr [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 1), %left ], [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 2), %right ]
  store i8 7, ptr %pointer_a, align 1
  store i8 7, ptr %pointer_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %initialize ], [ %next, %latch ]
  %loaded_a = load i8, ptr %pointer_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %pointer_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %pointer_a, align 1
  store i8 %poisoned_b, ptr %pointer_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_phi_correlated_reordered_cycle_carry(
    i8 %selector) {
entry:
  %choose_pointer = icmp eq i8 %selector, 0
  br i1 %choose_pointer, label %left, label %right

left:
  br label %initialize

right:
  br label %initialize

initialize:
  %pointer_a = phi ptr [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 0), %left ], [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 1), %right ]
  %pointer_b = phi ptr [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 2), %right ], [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 1), %left ]
  store i8 7, ptr %pointer_a, align 1
  store i8 7, ptr %pointer_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %initialize ], [ %next, %latch ]
  %loaded_a = load i8, ptr %pointer_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %pointer_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %pointer_a, align 1
  store i8 %poisoned_b, ptr %pointer_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_compatible_overlap_pointer_phi_domain_cycle_carry(
    i8 %selector) {
entry:
  %choose_pointer = icmp eq i8 %selector, 0
  br i1 %choose_pointer, label %left, label %right

left:
  br label %initialize

right:
  br label %initialize

initialize:
  %pointer_a = phi ptr [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 0), %left ], [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 1), %right ]
  %pointer_b = phi ptr [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 0), %left ], [
      getelementptr inbounds ([4 x i8], ptr @poison_memory_array,
          i64 0, i64 2), %right ]
  store i8 7, ptr %pointer_a, align 1
  store i8 7, ptr %pointer_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %initialize ], [ %next, %latch ]
  %loaded_a = load i8, ptr %pointer_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %pointer_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %pointer_a, align 1
  store i8 %poisoned_b, ptr %pointer_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_symbolic_index_interval_cycle_carry(
    i8 %selector) {
entry:
  %index = and i8 %selector, 1
  %pointer_a = getelementptr inbounds [2 x [2 x i8]],
      ptr @poison_memory_matrix, i64 0, i64 0, i8 %index
  %pointer_b = getelementptr inbounds [2 x [2 x i8]],
      ptr @poison_memory_matrix, i64 0, i64 1, i8 %index
  store i8 7, ptr %pointer_a, align 1
  store i8 7, ptr %pointer_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded_a = load i8, ptr %pointer_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %pointer_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %pointer_a, align 1
  store i8 %poisoned_b, ptr %pointer_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_overlap_symbolic_index_interval_cycle_carry(
    i8 %selector) {
entry:
  %index = and i8 %selector, 1
  %pointer_a = getelementptr inbounds [2 x [2 x i8]],
      ptr @poison_memory_matrix, i64 0, i64 0, i8 %index
  %pointer_b = getelementptr inbounds [2 x [2 x i8]],
      ptr @poison_memory_matrix, i64 0, i64 0, i8 %index
  store i8 7, ptr %pointer_a, align 1
  store i8 7, ptr %pointer_b, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded_a = load i8, ptr %pointer_a, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr %pointer_b, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned_a = add nsw i8 127, 1
  %poisoned_b = sub nsw i8 -128, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_both, label %carry_path

store_both:
  store i8 %poisoned_a, ptr %pointer_a, align 1
  store i8 %poisoned_b, ptr %pointer_b, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_same_cell_double_cycle_carry(i8 %selector) {
entry:
  store i8 7, ptr @poison_memory_slot, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %latch ]
  %loaded_a = load i8, ptr @poison_memory_slot, align 1
  %stable_a = freeze i8 %loaded_a
  %loaded_b = load i8, ptr @poison_memory_slot, align 1
  %stable_b = freeze i8 %loaded_b
  %poisoned = add nsw i8 127, 1
  %done = icmp eq i8 %iteration, 1
  br i1 %done, label %exit, label %choose

choose:
  %write = icmp eq i8 %selector, 0
  br i1 %write, label %store_value, label %carry_path

store_value:
  store i8 %poisoned, ptr @poison_memory_slot, align 1
  br label %latch

carry_path:
  br label %latch

latch:
  %next = add i8 %iteration, 1
  br label %loop

exit:
  %mixed = xor i8 %stable_a, %stable_b
  %matched = icmp eq i8 %mixed, 0
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bit_permutation_reverse(i8 %value) {
entry:
  %reversed = call i8 @llvm.bitreverse.i8(i8 %value)
  %matched = icmp eq i8 %reversed, 109
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bit_permutation_fshl() {
entry:
  %shifted = call i8 @llvm.fshl.i8(i8 -1, i8 0, i8 15)
  ret i8 %shifted
}

define i8 @bit_permutation_fshr() {
entry:
  %shifted = call i8 @llvm.fshr.i8(i8 0, i8 -1, i8 15)
  ret i8 %shifted
}

define i32 @saturating_pack() {
entry:
  %unsigned_add = call i8 @llvm.uadd.sat.i8(i8 250, i8 20)
  %unsigned_sub = call i8 @llvm.usub.sat.i8(i8 3, i8 5)
  %signed_add = call i8 @llvm.sadd.sat.i8(i8 120, i8 20)
  %signed_sub = call i8 @llvm.ssub.sat.i8(i8 -120, i8 20)
  %part0 = zext i8 %unsigned_add to i32
  %part1_raw = zext i8 %unsigned_sub to i32
  %part1 = shl i32 %part1_raw, 8
  %part2_raw = zext i8 %signed_add to i32
  %part2 = shl i32 %part2_raw, 16
  %part3_raw = zext i8 %signed_sub to i32
  %part3 = shl i32 %part3_raw, 24
  %low = or i32 %part0, %part1
  %high = or i32 %part2, %part3
  %packed = or i32 %low, %high
  ret i32 %packed
}

define i8 @saturating_symbolic(i8 %value) {
entry:
  %sum = call i8 @llvm.uadd.sat.i8(i8 %value, i8 10)
  %clamped = icmp eq i8 %sum, -1
  br i1 %clamped, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i16 @saturating_shift_pack() {
entry:
  %signed = call i8 @llvm.sshl.sat.i8(i8 -100, i8 2)
  %unsigned = call i8 @llvm.ushl.sat.i8(i8 100, i8 2)
  %low = zext i8 %signed to i16
  %high_raw = zext i8 %unsigned to i16
  %high = shl i16 %high_raw, 8
  %packed = or i16 %low, %high
  ret i16 %packed
}

define i8 @bad_saturating_shift() {
entry:
  %poisoned = call i8 @llvm.ushl.sat.i8(i8 1, i8 8)
  ret i8 %poisoned
}

define i8 @overflow_arithmetic_pack() {
entry:
  %uadd = call {i8, i1} @llvm.uadd.with.overflow.i8(i8 250, i8 20)
  %uadd_value = extractvalue {i8, i1} %uadd, 0
  %uadd_flag = extractvalue {i8, i1} %uadd, 1
  %uadd_value_ok = icmp eq i8 %uadd_value, 14
  %uadd_ok = and i1 %uadd_value_ok, %uadd_flag
  %usub = call {i8, i1} @llvm.usub.with.overflow.i8(i8 3, i8 5)
  %usub_value = extractvalue {i8, i1} %usub, 0
  %usub_flag = extractvalue {i8, i1} %usub, 1
  %usub_value_ok = icmp eq i8 %usub_value, -2
  %usub_ok = and i1 %usub_value_ok, %usub_flag
  %umul = call {i8, i1} @llvm.umul.with.overflow.i8(i8 20, i8 20)
  %umul_value = extractvalue {i8, i1} %umul, 0
  %umul_flag = extractvalue {i8, i1} %umul, 1
  %umul_value_ok = icmp eq i8 %umul_value, -112
  %umul_ok = and i1 %umul_value_ok, %umul_flag
  %sadd = call {i8, i1} @llvm.sadd.with.overflow.i8(i8 120, i8 20)
  %sadd_value = extractvalue {i8, i1} %sadd, 0
  %sadd_flag = extractvalue {i8, i1} %sadd, 1
  %sadd_value_ok = icmp eq i8 %sadd_value, -116
  %sadd_ok = and i1 %sadd_value_ok, %sadd_flag
  %ssub = call {i8, i1} @llvm.ssub.with.overflow.i8(i8 -120, i8 20)
  %ssub_value = extractvalue {i8, i1} %ssub, 0
  %ssub_flag = extractvalue {i8, i1} %ssub, 1
  %ssub_value_ok = icmp eq i8 %ssub_value, 116
  %ssub_ok = and i1 %ssub_value_ok, %ssub_flag
  %smul = call {i8, i1} @llvm.smul.with.overflow.i8(i8 20, i8 20)
  %smul_value = extractvalue {i8, i1} %smul, 0
  %smul_flag = extractvalue {i8, i1} %smul, 1
  %smul_value_ok = icmp eq i8 %smul_value, -112
  %smul_ok = and i1 %smul_value_ok, %smul_flag
  %zero = call {i8, i1} @llvm.umul.with.overflow.i8(i8 42, i8 0)
  %zero_value = extractvalue {i8, i1} %zero, 0
  %zero_flag = extractvalue {i8, i1} %zero, 1
  %zero_value_ok = icmp eq i8 %zero_value, 0
  %zero_flag_ok = xor i1 %zero_flag, true
  %zero_ok = and i1 %zero_value_ok, %zero_flag_ok
  %pair0 = and i1 %uadd_ok, %usub_ok
  %pair1 = and i1 %umul_ok, %sadd_ok
  %pair2 = and i1 %ssub_ok, %smul_ok
  %first = and i1 %pair0, %pair1
  %second = and i1 %pair2, %zero_ok
  %all = and i1 %first, %second
  br i1 %all, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @overflow_arithmetic_symbolic(i8 %value) {
entry:
  %sum = call {i8, i1} @llvm.uadd.with.overflow.i8(i8 %value, i8 10)
  %overflow = extractvalue {i8, i1} %sum, 1
  br i1 %overflow, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @overflow_signed_minimum_multiply() {
entry:
  %product = call {i8, i1} @llvm.smul.with.overflow.i8(i8 -128, i8 -1)
  %value = extractvalue {i8, i1} %product, 0
  %overflow = extractvalue {i8, i1} %product, 1
  %value_ok = icmp eq i8 %value, -128
  %ok = and i1 %value_ok, %overflow
  %result = select i1 %ok, i8 1, i8 2
  ret i8 %result
}

define i64 @scalar_selection_pack() {
entry:
  %absolute = call i8 @llvm.abs.i8(i8 -5, i1 false)
  %minimum = call i8 @llvm.abs.i8(i8 -128, i1 false)
  %signed_max = call i8 @llvm.smax.i8(i8 -3, i8 2)
  %signed_min = call i8 @llvm.smin.i8(i8 -3, i8 2)
  %unsigned_max = call i8 @llvm.umax.i8(i8 -6, i8 2)
  %unsigned_min = call i8 @llvm.umin.i8(i8 -6, i8 2)
  %part0 = zext i8 %absolute to i64
  %part1_raw = zext i8 %minimum to i64
  %part1 = shl i64 %part1_raw, 8
  %part2_raw = zext i8 %signed_max to i64
  %part2 = shl i64 %part2_raw, 16
  %part3_raw = zext i8 %signed_min to i64
  %part3 = shl i64 %part3_raw, 24
  %part4_raw = zext i8 %unsigned_max to i64
  %part4 = shl i64 %part4_raw, 32
  %part5_raw = zext i8 %unsigned_min to i64
  %part5 = shl i64 %part5_raw, 40
  %merge01 = or i64 %part0, %part1
  %merge23 = or i64 %part2, %part3
  %merge45 = or i64 %part4, %part5
  %merge0123 = or i64 %merge01, %merge23
  %packed = or i64 %merge0123, %merge45
  ret i64 %packed
}

define i8 @scalar_selection_symbolic(i8 %value) {
entry:
  %maximum = call i8 @llvm.smax.i8(i8 %value, i8 10)
  %selected = icmp eq i8 %maximum, %value
  br i1 %selected, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_scalar_abs_poison() {
entry:
  %poisoned = call i8 @llvm.abs.i8(i8 -128, i1 true)
  ret i8 %poisoned
}

define i8 @optimization_hints(i8 %value) {
entry:
  %expected = call i8 @llvm.expect.i8(i8 %value, i8 10)
  %probable = call i8 @llvm.expect.with.probability.i8(
      i8 %expected, i8 10, double 9.000000e-01)
  %matched = icmp eq i8 %probable, 10
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i64 @objectsize_stack() {
entry:
  %object = alloca [8 x i8], align 1
  %pointer = getelementptr [8 x i8], ptr %object, i64 0, i64 2
  %size = call i64 @llvm.objectsize.i64.p0(
      ptr %pointer, i1 false, i1 true, i1 false)
  ret i64 %size
}

define i8 @objectsize_union(i1 %choose) {
entry:
  %left = getelementptr [4 x i8], ptr @objectsize_left, i64 0, i64 1
  %right = getelementptr [6 x i8], ptr @objectsize_right, i64 0, i64 2
  %pointer = select i1 %choose, ptr %left, ptr %right
  %size = call i64 @llvm.objectsize.i64.p0(
      ptr %pointer, i1 false, i1 true, i1 false)
  %left_size = icmp eq i64 %size, 3
  br i1 %left_size, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i64 @objectsize_null() {
entry:
  %size = call i64 @llvm.objectsize.i64.p0(
      ptr null, i1 false, i1 false, i1 false)
  ret i64 %size
}

define i64 @objectsize_dynamic_input(ptr %data, i64 %length) {
entry:
  %interior = getelementptr i8, ptr %data, i64 1
  %size = call i64 @llvm.objectsize.i64.p0(
      ptr %interior, i1 false, i1 false, i1 true)
  ret i64 %size
}

define i64 @objectsize_dynamic_heap(i64 %length) {
entry:
  %memory = call ptr @malloc(i64 %length)
  %size = call i64 @llvm.objectsize.i64.p0(
      ptr %memory, i1 false, i1 false, i1 true)
  ret i64 %size
}

define i64 @objectsize_static_runtime(i64 %length) {
entry:
  %memory = call ptr @malloc(i64 %length)
  %size = call i64 @llvm.objectsize.i64.p0(
      ptr %memory, i1 false, i1 true, i1 false)
  ret i64 %size
}

define i64 @objectsize_dynamic_realloc(i64 %length) {
entry:
  %memory = call ptr @malloc(i64 4)
  %resized = call ptr @realloc(ptr %memory, i64 %length)
  %size = call i64 @llvm.objectsize.i64.p0(
      ptr %resized, i1 false, i1 false, i1 true)
  ret i64 %size
}

define i8 @objectsize_dynamic_realloc_union(i1 %choose, i64 %length) {
entry:
  %memory = call ptr @malloc(i64 4)
  %resized = call ptr @realloc(ptr %memory, i64 %length)
  %selected = select i1 %choose, ptr %resized, ptr @objectsize_left
  %size = call i64 @llvm.objectsize.i64.p0(
      ptr %selected, i1 false, i1 false, i1 true)
  %is_resized = icmp eq i64 %size, 2
  br i1 %is_resized, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @pointer_memory_global() {
entry:
  %pointer = load ptr, ptr @pointer_memory_slot, align 8
  %value = load i8, ptr %pointer, align 1
  ret i8 %value
}

define i8 @pointer_memory_stack(i1 %choose) {
entry:
  %cell = alloca [1 x ptr], align 8
  %slot = getelementptr [1 x ptr], ptr %cell, i64 0, i64 0
  %selected = select i1 %choose, ptr @union_left, ptr @union_right
  store ptr %selected, ptr %slot, align 8
  %pointer = load ptr, ptr %slot, align 8
  %value = load i8, ptr %pointer, align 1
  %left = icmp eq i8 %value, 65
  br i1 %left, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @pointer_memory_symbolic_gep(i8 %index) {
entry:
  %pointer = getelementptr inbounds [2 x i8],
      ptr @symbolic_pointer_bytes, i64 0, i8 %index
  store ptr %pointer, ptr @symbolic_pointer_slot, align 8
  %loaded_pointer = load ptr, ptr @symbolic_pointer_slot, align 8
  %value = load i8, ptr %loaded_pointer, align 1
  %is_a = icmp eq i8 %value, 65
  br i1 %is_a, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @pointer_memory_heap() {
entry:
  %cell = call ptr @malloc(i64 8)
  store ptr @union_left_array, ptr %cell, align 8
  %base = load ptr, ptr %cell, align 8
  %slot = getelementptr [2 x i8], ptr %base, i64 0, i64 1
  %value = load i8, ptr %slot, align 1
  call void @free(ptr %cell)
  ret i8 %value
}

define i8 @pointer_memory_null() {
entry:
  %cell = alloca [1 x ptr], align 8
  %slot = getelementptr [1 x ptr], ptr %cell, i64 0, i64 0
  store ptr null, ptr %slot, align 8
  %pointer = load ptr, ptr %slot, align 8
  %missing = icmp eq ptr %pointer, null
  %result = zext i1 %missing to i8
  ret i8 %result
}

define i8 @pointer_memory_conditional(i1 %choose) {
entry:
  %cell = alloca [1 x ptr], align 8
  %slot = getelementptr [1 x ptr], ptr %cell, i64 0, i64 0
  br i1 %choose, label %left, label %right

left:
  store ptr @union_left, ptr %slot, align 8
  br label %merge

right:
  store ptr @union_right, ptr %slot, align 8
  br label %merge

merge:
  %pointer = load ptr, ptr %slot, align 8
  %value = load i8, ptr %pointer, align 1
  %is_left = icmp eq i8 %value, 65
  br i1 %is_left, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @pointer_memory_nested_merge(
    i1 %outer, i1 %inner) {
entry:
  %cell = alloca [1 x ptr], align 8
  %slot = getelementptr [1 x ptr], ptr %cell, i64 0, i64 0
  store ptr @union_left, ptr %slot, align 8
  br i1 %outer, label %nested, label %bypass

nested:
  br i1 %inner, label %overwrite, label %forward

overwrite:
  store ptr @union_right, ptr %slot, align 8
  br label %inner_join

forward:
  br label %inner_join

inner_join:
  br label %merge

bypass:
  br label %merge

merge:
  %pointer = load ptr, ptr %slot, align 8
  %value = load i8, ptr %pointer, align 1
  %is_left = icmp eq i8 %value, 65
  br i1 %is_left, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @function_pointer_memory_conditional(
    i1 %choose, i8 %value) {
entry:
  %cell = alloca [1 x ptr], align 8
  %slot = getelementptr [1 x ptr], ptr %cell, i64 0, i64 0
  br i1 %choose, label %left, label %right

left:
  store ptr @increment, ptr %slot, align 8
  br label %merge

right:
  store ptr @increment_two, ptr %slot, align 8
  br label %merge

merge:
  %target = load ptr, ptr %slot, align 8
  %result = call i8 %target(i8 %value)
  ret i8 %result
}

define i8 @function_pointer_memory_overwrite(
    i1 %choose, i8 %value) {
entry:
  %selected = select i1 %choose, ptr @increment, ptr @increment_two
  store ptr %selected, ptr @function_pointer_memory_slot, align 8
  %target = load ptr, ptr @function_pointer_memory_slot, align 8
  %result = call i8 %target(i8 %value)
  ret i8 %result
}

define i8 @function_pointer_memory_initial_merge(
    i1 %choose, i8 %value) {
entry:
  br i1 %choose, label %write, label %keep

write:
  store ptr @increment_two, ptr @function_pointer_memory_slot, align 8
  br label %merge

keep:
  br label %merge

merge:
  %target = load ptr, ptr @function_pointer_memory_slot, align 8
  %result = call i8 %target(i8 %value)
  ret i8 %result
}

define i8 @bad_pointer_memory_incomplete(i1 %choose) {
entry:
  %cell = alloca [1 x ptr], align 8
  %slot = getelementptr [1 x ptr], ptr %cell, i64 0, i64 0
  br i1 %choose, label %left, label %right

left:
  store ptr @union_left, ptr %slot, align 8
  br label %merge

right:
  br label %merge

merge:
  %pointer = load ptr, ptr %slot, align 8
  %value = load i8, ptr %pointer, align 1
  ret i8 %value
}

define i8 @pointer_memory_cycle() {
entry:
  %cell = alloca [1 x ptr], align 8
  %slot = getelementptr [1 x ptr], ptr %cell, i64 0, i64 0
  store ptr @union_left, ptr %slot, align 8
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %loop ]
  %pointer = load ptr, ptr %slot, align 8
  %next = add i8 %iteration, 1
  %repeat = icmp ult i8 %next, 3
  br i1 %repeat, label %loop, label %exit

exit:
  %value = load i8, ptr %pointer, align 1
  ret i8 %value
}

define i8 @pointer_memory_cycle_write() {
entry:
  %cell = alloca [1 x ptr], align 8
  %slot = getelementptr [1 x ptr], ptr %cell, i64 0, i64 0
  store ptr @union_left, ptr %slot, align 8
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %loop ]
  %pointer = load ptr, ptr %slot, align 8
  store ptr @union_right, ptr %slot, align 8
  %next = add i8 %iteration, 1
  %repeat = icmp ult i8 %next, 3
  br i1 %repeat, label %loop, label %exit

exit:
  %value = load i8, ptr %pointer, align 1
  ret i8 %value
}

define i8 @pointer_memory_equivalent_gep() {
entry:
  %cell = alloca [2 x ptr], align 8
  %writer_slot = getelementptr [2 x ptr], ptr %cell, i64 0, i64 1
  store ptr @union_left, ptr %writer_slot, align 8
  %reader_slot = getelementptr [2 x ptr], ptr %cell, i64 0, i64 1
  %pointer = load ptr, ptr %reader_slot, align 8
  %value = load i8, ptr %pointer, align 1
  ret i8 %value
}

define i8 @pointer_memory_distinct_gep() {
entry:
  %cell = alloca [2 x ptr], align 8
  %left_slot = getelementptr [2 x ptr], ptr %cell, i64 0, i64 1
  %right_slot = getelementptr [2 x ptr], ptr %cell, i64 0, i64 0
  store ptr @union_left, ptr %left_slot, align 8
  store ptr @union_right, ptr %right_slot, align 8
  %reader_slot = getelementptr [2 x ptr], ptr %cell, i64 0, i64 1
  %pointer = load ptr, ptr %reader_slot, align 8
  %value = load i8, ptr %pointer, align 1
  ret i8 %value
}

define i8 @bad_pointer_memory_uninitialized_cycle() {
entry:
  %cell = alloca [1 x ptr], align 8
  %slot = getelementptr [1 x ptr], ptr %cell, i64 0, i64 0
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ %next, %loop ]
  %pointer = load ptr, ptr %slot, align 8
  %next = add i8 %iteration, 1
  %repeat = icmp ult i8 %next, 3
  br i1 %repeat, label %loop, label %exit

exit:
  %value = load i8, ptr %pointer, align 1
  ret i8 %value
}

define i8 @bad_pointer_memory_overwrite() {
entry:
  %cell = alloca [1 x ptr], align 8
  %slot = getelementptr [1 x ptr], ptr %cell, i64 0, i64 0
  store ptr @union_left, ptr %slot, align 8
  store i64 0, ptr %slot, align 8
  %pointer = load ptr, ptr %slot, align 8
  %value = load i8, ptr %pointer, align 1
  ret i8 %value
}

define i8 @function_pointer_memory_global(i8 %value) {
entry:
  %target = load ptr, ptr @function_pointer_memory_slot, align 8
  %result = call i8 %target(i8 %value)
  ret i8 %result
}

define i8 @function_pointer_memory_stack(i1 %choose, i8 %value) {
entry:
  %cell = alloca [1 x ptr], align 8
  %slot = getelementptr [1 x ptr], ptr %cell, i64 0, i64 0
  %selected = select i1 %choose, ptr @increment, ptr @increment_two
  store ptr %selected, ptr %slot, align 8
  %target = load ptr, ptr %slot, align 8
  %result = call i8 %target(i8 %value)
  ret i8 %result
}

define i8 @data_pointer_table(i8 %index) {
entry:
  %bounded = and i8 %index, 1
  %wide = zext i8 %bounded to i64
  %slot = getelementptr inbounds [2 x ptr],
      ptr @data_pointer_table_values, i64 0, i64 %wide
  %pointer = load ptr, ptr %slot, align 8
  %value = load i8, ptr %pointer, align 1
  %left = icmp eq i8 %value, 65
  br i1 %left, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @function_pointer_table(i8 %index, i8 %value) {
entry:
  %bounded = and i8 %index, 1
  %wide = zext i8 %bounded to i64
  %slot = getelementptr inbounds [2 x ptr],
      ptr @function_pointer_table_values, i64 0, i64 %wide
  %target = load ptr, ptr %slot, align 8
  %result = call i8 %target(i8 %value)
  ret i8 %result
}

define i8 @pointer_select_null(i1 %choose) {
entry:
  %pointer = select i1 %choose, ptr @union_left, ptr null
  %value = load i8, ptr %pointer, align 1
  %matched = icmp eq i8 %value, 65
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 0
}

define i8 @bad_pointer_union_wide(i1 %choose, i8 %index) {
entry:
  %left_pointer = getelementptr [9 x i8], ptr @union_wide_left, i64 0, i8 %index
  %right_pointer = getelementptr [9 x i8], ptr @union_wide_right, i64 0, i8 %index
  %pointer = select i1 %choose, ptr %left_pointer, ptr %right_pointer
  %value = load i8, ptr %pointer, align 1
  ret i8 %value
}

define i8 @bad_pointer_phi_cycle(i1 %repeat) {
entry:
  br label %loop

loop:
  %pointer = phi ptr [ @union_left, %entry ], [ %pointer, %loop ]
  br i1 %repeat, label %loop, label %exit

exit:
  %value = load i8, ptr %pointer, align 1
  ret i8 %value
}

define i8 @freeze_memory_composed_trigroup_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_first, i1 %first_leaf, i1 %first_inner,
    i1 %choose_second, i1 %second_leaf, i1 %choose_third,
    i1 %third_leaf) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %first_override_pointer = getelementptr i8, ptr %pointer, i64 1
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  %third_pointer = getelementptr i8, ptr %pointer, i64 3
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_first, label %first_group, label %second_tail

first_group:
  %first_poison = add nsw i16 32767, 1
  store i16 %first_poison, ptr %pointer, align 1
  br i1 %first_leaf, label %first_leaf_a, label %first_inner_branch

first_inner_branch:
  br i1 %first_inner, label %first_leaf_b, label %first_override_leaf

second_tail:
  br i1 %choose_second, label %second_group, label %third_tail

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 %second_leaf, label %second_leaf_a, label %second_leaf_b

third_tail:
  br i1 %choose_third, label %third_group, label %carry_leaf

third_group:
  %third_poison = add nsw i8 127, 1
  store i8 %third_poison, ptr %third_pointer, align 1
  br i1 %third_leaf, label %third_leaf_a, label %third_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

first_override_leaf:
  %first_override_poison = add nsw i8 127, 1
  store i8 %first_override_poison, ptr %first_override_pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

third_leaf_a:
  br label %latch

third_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_composed_trigroup_recursive_conditional_cyclic_byte_lane_full_shadow() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  %third_pointer = getelementptr i8, ptr %pointer, i64 3
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_group, label %second_tail

first_group:
  %first_poison = add nsw i16 32767, 1
  store i16 %first_poison, ptr %pointer, align 1
  br i1 false, label %first_leaf_a, label %first_inner_branch

first_inner_branch:
  br i1 false, label %first_leaf_b, label %first_override_leaf

second_tail:
  br i1 true, label %second_group, label %third_tail

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 true, label %second_leaf_a, label %second_leaf_b

third_tail:
  br i1 true, label %third_group, label %carry_leaf

third_group:
  %third_poison = add nsw i8 127, 1
  store i8 %third_poison, ptr %third_pointer, align 1
  br i1 true, label %third_leaf_a, label %third_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

first_override_leaf:
  %first_override_poison = add nsw i16 32767, 1
  store i16 %first_override_poison, ptr %pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

third_leaf_a:
  br label %latch

third_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @freeze_memory_forwarded_composed_trigroup_recursive_conditional_cyclic_byte_lane_carry(
    i1 %choose_first, i1 %first_leaf, i1 %first_inner,
    i1 %choose_second, i1 %second_leaf, i1 %choose_third,
    i1 %third_leaf) {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %first_override_pointer = getelementptr i8, ptr %pointer, i64 1
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  %third_pointer = getelementptr i8, ptr %pointer, i64 3
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 %choose_first, label %first_group_head, label %second_tail

first_group_head:
  br label %first_group

first_group:
  %first_poison = add nsw i16 32767, 1
  store i16 %first_poison, ptr %pointer, align 1
  br i1 %first_leaf, label %first_leaf_a, label %first_inner_branch

first_inner_branch:
  br i1 %first_inner, label %first_leaf_b, label %first_override_leaf

second_tail:
  br i1 %choose_second, label %second_group, label %third_tail

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 %second_leaf, label %second_leaf_a, label %second_leaf_b

third_tail:
  br i1 %choose_third, label %third_group, label %carry_leaf

third_group:
  %third_poison = add nsw i8 127, 1
  store i8 %third_poison, ptr %third_pointer, align 1
  br i1 %third_leaf, label %third_leaf_a, label %third_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

first_override_leaf:
  %first_override_poison = add nsw i8 127, 1
  store i8 %first_override_poison, ptr %first_override_pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

third_leaf_a:
  br label %latch

third_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @bad_freeze_memory_forwarded_composed_trigroup_recursive_conditional_cyclic_byte_lane_nested() {
entry:
  %pointer = getelementptr [4 x i8],
      ptr @poison_memory_array, i64 0, i64 0
  %first_override_pointer = getelementptr i8, ptr %pointer, i64 1
  %second_pointer = getelementptr i8, ptr %pointer, i64 2
  %third_pointer = getelementptr i8, ptr %pointer, i64 3
  store i32 42, ptr %pointer, align 1
  br label %loop

loop:
  %iteration = phi i8 [ 0, %entry ], [ 1, %latch ]
  %loaded = load i32, ptr %pointer, align 1
  %stable = freeze i32 %loaded
  %first = icmp eq i8 %iteration, 0
  br i1 %first, label %body, label %exit

body:
  br i1 true, label %first_group_head, label %second_tail

first_group_head:
  br i1 true, label %first_group, label %first_group_detour

first_group_detour:
  br label %first_group

first_group:
  %first_poison = add nsw i16 32767, 1
  store i16 %first_poison, ptr %pointer, align 1
  br i1 false, label %first_leaf_a, label %first_inner_branch

first_inner_branch:
  br i1 false, label %first_leaf_b, label %first_override_leaf

second_tail:
  br i1 true, label %second_group, label %third_tail

second_group:
  %second_poison = sub nsw i8 -128, 1
  store i8 %second_poison, ptr %second_pointer, align 1
  br i1 true, label %second_leaf_a, label %second_leaf_b

third_tail:
  br i1 true, label %third_group, label %carry_leaf

third_group:
  %third_poison = add nsw i8 127, 1
  store i8 %third_poison, ptr %third_pointer, align 1
  br i1 true, label %third_leaf_a, label %third_leaf_b

first_leaf_a:
  br label %latch

first_leaf_b:
  br label %latch

first_override_leaf:
  %first_override_poison = add nsw i8 127, 1
  store i8 %first_override_poison, ptr %first_override_pointer, align 1
  br label %latch

second_leaf_a:
  br label %latch

second_leaf_b:
  br label %latch

third_leaf_a:
  br label %latch

third_leaf_b:
  br label %latch

carry_leaf:
  br label %latch

latch:
  br label %loop

exit:
  %matched = icmp eq i32 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}
