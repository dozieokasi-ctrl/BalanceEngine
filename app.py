print("Welcome to BalanceEngine")

task_name = input("What task do you need to complete? ")
estimated_hours = float(input("How many hours will the task take? "))
days_left = int(input("How many days until it is due? "))

hours_per_day = estimated_hours / days_left

print(f"Task added: {task_name}")
print(f"Work on this task for {hours_per_day:.1f} hours per day.")