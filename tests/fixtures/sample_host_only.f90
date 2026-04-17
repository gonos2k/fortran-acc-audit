! Fixture without any OpenACC — should produce zero violations and zero
! acc_kernel_call edges. Exercises the .f90 (lowercase, no-CPP) path.

module sample_host_module
  implicit none
contains

  subroutine host_driver(n, x)
    integer, intent(in) :: n
    real, intent(inout) :: x(n)
    integer :: i
    do i = 1, n
      call increment(x(i))
    end do
  end subroutine host_driver

  subroutine increment(a)
    real, intent(inout) :: a
    a = a + 1.0
  end subroutine increment

end module sample_host_module
