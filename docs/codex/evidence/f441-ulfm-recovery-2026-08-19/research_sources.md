# F441 主要研究与实现资料

1. W. Bland et al., “Post-failure recovery of MPI communication capability:
   Design and rationale,” IJHPCA 2013.
   DOI: https://doi.org/10.1177/1094342013488238
   采用：revoke、shrink、failure acknowledgement与幸存通信能力恢复的基本语义。
2. Open MPI 5.0.10 ULFM documentation:
   https://docs.open-mpi.org/en/v5.0.10/features/ulfm.html
   采用：`--with-ft=ulfm`构建、`--with-ft ulfm`运行和Open MPI 5.0.x API合同。
3. Open MPI 5.0.10 source release:
   https://download.open-mpi.org/release/open-mpi/v5.0/openmpi-5.0.10.tar.gz
   固定SHA-256：`5692cc80554a7117c99eaa725d35100edd8bbf73423a5e265ff867979192df7d`。
4. MPI Forum Fault Tolerance Working Group:
   https://www.mpi-forum.org/working-groups/ft/
   用于区分标准化讨论、ULFM实现和本项目附加协议。

本项目新增而非上游直接提供的合同：稳定endpoint/incarnation身份、generation/shard/lease
三级栅栏、所有在途任务保守重放、两阶段有界规范JSON attestation、严格恢复receipt与三层oracle。

范围纪律：这些资料不支持主执行器已经透明容错、跨节点性能提升、coverage提升或defect-yield提升；
对应结论必须等待生产接线和独立R级实验。
