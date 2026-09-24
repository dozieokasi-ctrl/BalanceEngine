print("Welcome to BalanceEngine")

task_name = input("What task do you need to complete? ")
estimated_hours = float(input("How many hours will the task take? "))
days_left = int(input("How many days until it is due? "))
importance = int(input("How important is this task from 1 to 5? "))

if days_left <= 0:
    print("Days left must be greater than zero.")

elif importance < 1 or importance > 5:
    print("Importance must be between 1 and 5.")

else:
    hours_per_day = estimated_hours / days_left
    priority_score = importance * hours_per_day

    if priority_score >= 10:
        priority_level = "High"
    elif priority_score >= 5:
        priority_level = "Medium"
    else:
        priority_level = "Low"

    print(f"\nTask added: {task_name}")
    print(f"Work on this task for {hours_per_day:.1f} hours per day.")
    print(f"Priority score: {priority_score:.1f}")
    print(f"Priority level: {priority_level}")