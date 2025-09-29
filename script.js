async function loadData() {
  const response = await fetch('showtimes.json');
  const data = await response.json();

  const tbody = document.querySelector("#showtimesTable tbody");
  const searchInput = document.getElementById("search");

  let rows = [];

  // Flatten JSON into table rows
  for (const theater in data) {
    for (const movie of data[theater]) {
      for (const showtime of movie.showtimes) {
        rows.push({
          theater,
          title: movie.title,
          runtime: movie.runtime ? `${Math.floor(movie.runtime / 60)}h ${movie.runtime % 60}m` : "Unknown",
          datetime: new Date(showtime.datetime).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }),
          label: showtime.label || ""
        });
      }
    }
  }

  function renderTable(filteredRows) {
    tbody.innerHTML = filteredRows.map(r => `
      <tr>
        <td>${r.theater}</td>
        <td class="movie-title">${r.title}</td>
        <td class="runtime">${r.runtime}</td>
        <td>${r.datetime}</td>
        <td>${r.label}</td>
      </tr>
    `).join("");
  }

  renderTable(rows);

  // Live search
  searchInput.addEventListener("input", e => {
    const q = e.target.value.toLowerCase();
    const filtered = rows.filter(r =>
      r.theater.toLowerCase().includes(q) ||
      r.title.toLowerCase().includes(q) ||
      r.label.toLowerCase().includes(q)
    );
    renderTable(filtered);
  });
}

loadData();
