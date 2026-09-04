; RUN: %python -c "from pathlib import Path; source=Path(r'%S/live_nested_loop_memoryphi_executable_transfer.ll').read_text(); source=source.replace('ret i16 %value', '%combined = add i16 %value, %outer_iv\n  ret i16 %combined'); Path(r'%t.liveout.ll').write_text(source)"
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.liveout.ll --output %t.json --entry executable_nested_summary_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --validate-only --expect-stack --expect-alias --expect-nested-loop-memoryphi-decision-dag-value-summary --reject-capability refinement-verified-nested-loop-memory-summary-transfer

; A scalar loop live-out must preserve the original loop.  The generated
; module otherwise matches the executable-transfer fixture exactly.
