print("Welcome to BalanceEngine")

tasks = []

number_of_tasks = int(input("How many tasks do you want to add? "))

for task_number in range(number_of_tasks):
    print(f"\n--- Task {task_number + 1} ---")

    task_name = input("What task do you need to complete? ")
    estimated_hours = float(input("How many hours will the task take? "))
    days_left = int(input("How many days until it is due? "))
    importance = int(input("How important is this task from 1 to 5? "))

    if days_left <= 0:
        print("Days left must be greater than zero.")
        continue

    elif importance < 1 or importance > 5:
        print("Importance must be between 1 and 5.")
        continue

    hours_per_day = estimated_hours / days_left
    priority_score = importance * hours_per_day

    if priority_score >= 10:
        priority_level = "High"
    elif priority_score >= 5:
        priority_level = "Medium"
    else:
        priority_level = "Low"

    task = {
        "name": task_name,
        "hours_per_day": hours_per_day,
        "priority_score": priority_score,
        "priority_level": priority_level
    }

    tasks.append(task)
    print(f"{task_name} has been saved.")

if len(tasks) == 0:
    print("\nNo valid tasks were entered.")

else:
    tasks.sort(
        key=lambda task: task["priority_score"],
        reverse=True
    )

    print("\n=== YOUR PRIORITY PLAN ===")

    for rank, task in enumerate(tasks, start=1):
        print(f"\n{rank}. {task['name']}")
        print(f"   Priority: {task['priority_level']}")
        print(f"   Score: {task['priority_score']:.1f}")
        print(f"   Daily work: {task['hours_per_day']:.1f} hours")